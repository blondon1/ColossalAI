import argparse
import os
import resource
from contextlib import contextmanager
from copy import deepcopy

import psutil
import torch
import torch.distributed as dist
import torch.nn as nn
from coati.models.base import RewardModel
from coati.models.bloom import BLOOMActor, BLOOMCritic
from coati.trainer import PPOTrainer
from coati.trainer.callbacks import PerformanceEvaluator
from coati.trainer.strategies import ColossalAIStrategy, Strategy, TPZeroStrategy
from torch.optim import Adam
from torch.utils.data import DataLoader
from transformers import AutoTokenizer
from transformers.modeling_utils import no_init_weights
from transformers.models.bloom.configuration_bloom import BloomConfig

from colossalai.nn.optimizer import HybridAdam


def get_model_numel(model: nn.Module, strategy: Strategy) -> int:
    numel = sum(p.numel() for p in model.parameters())
    if isinstance(strategy, ColossalAIStrategy) and strategy.stage == 3 and strategy.shard_init:
        numel *= dist.get_world_size()
    return numel


def preprocess_batch(samples) -> dict:
    input_ids = torch.stack(samples)
    attention_mask = torch.ones_like(input_ids, dtype=torch.long)
    return {'input_ids': input_ids, 'attention_mask': attention_mask}


def preprocess_ptx_batch(samples) -> dict:
    batch = preprocess_batch(samples)
    batch['labels'] = batch['input_ids']
    return batch


def print_rank_0(*args, **kwargs) -> None:
    if dist.get_rank() == 0:
        print(*args, **kwargs)


def get_max_memory() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


@contextmanager
def low_precision_init(target_dtype: torch.dtype = torch.float16):
    dtype = torch.get_default_dtype()
    try:
        torch.set_default_dtype(target_dtype)
        yield
    finally:
        torch.set_default_dtype(dtype)


def print_model_numel(model_dict: dict) -> None:
    B = 1024**3
    M = 1024**2
    K = 1024
    outputs = ''
    for name, numel in model_dict.items():
        outputs += f'{name}: '
        if numel >= B:
            outputs += f'{numel / B:.2f} B\n'
        elif numel >= M:
            outputs += f'{numel / M:.2f} M\n'
        elif numel >= K:
            outputs += f'{numel / K:.2f} K\n'
        else:
            outputs += f'{numel}\n'
    print_rank_0(outputs)


def get_gpt_config(model_name: str) -> BloomConfig:
    model_map = {
        '350m': BloomConfig(hidden_size=1024, n_layer=24, n_head=16),
        '560m': BloomConfig.from_pretrained('bigscience/bloom-560m'),
        '1.1b': BloomConfig.from_pretrained('bigscience/bloom-1b1'),
        '1.7b': BloomConfig.from_pretrained('bigscience/bloom-1b7'),
        '3b': BloomConfig.from_pretrained('bigscience/bloom-3b'),
        '7b': BloomConfig.from_pretrained('bigscience/bloom-7b1'),
        '66b': BloomConfig(hidden_size=9216, n_layer=64, n_head=72),
        '175b': BloomConfig(hidden_size=12288, n_layer=96, n_head=128),
    }
    try:
        return model_map[model_name]
    except KeyError:
        raise ValueError(f'Unknown model "{model_name}"')


def main(args):
    if args.strategy == 'colossalai_gemini':
        strategy = ColossalAIStrategy(stage=3, placement_policy='cuda', initial_scale=2**5)
    elif args.strategy == 'colossalai_gemini_cpu':
        strategy = ColossalAIStrategy(stage=3, placement_policy='cpu', initial_scale=2**5)
    elif args.strategy == 'colossalai_gemini_reshard':
        strategy = ColossalAIStrategy(stage=3, placement_policy='cuda_reshard', initial_scale=2**5)
    elif args.strategy == 'tp_zero2':
        strategy = TPZeroStrategy(args.tp_size, zero_stage=2, initial_scale=2**5)
    elif args.strategy == 'tp_zero2_cpu':
        strategy = TPZeroStrategy(args.tp_size, zero_stage=2, initial_scale=2**5, cpu_offload=True)
    else:
        raise ValueError(f'Unsupported strategy "{args.strategy}"')

    torch.cuda.set_per_process_memory_fraction(args.cuda_mem_frac)

    model_config = get_gpt_config(args.model)
    critic_config = get_gpt_config(args.critic_model)
    with strategy.model_init_context(), no_init_weights(), low_precision_init():
        actor = BLOOMActor(config=model_config, lora_rank=args.lora_rank, checkpoint=args.grad_checkpoint)
        actor.model.tie_weights()
        critic = BLOOMCritic(config=critic_config, lora_rank=args.lora_rank, checkpoint=args.grad_checkpoint)
        critic.model.tie_weights()

        initial_model = BLOOMActor(config=model_config, lora_rank=args.lora_rank, checkpoint=args.grad_checkpoint)
        initial_model.model.tie_weights()
        reward_model = BLOOMCritic(config=critic_config, lora_rank=args.lora_rank, checkpoint=args.grad_checkpoint)
        reward_model.model.tie_weights()
        reward_model = RewardModel(reward_model.model, reward_model.value_head)

    if args.use_kernels:
        from coati.kernels import convert_to_xformer_model
        actor, critic, initial_model, reward_model = map(convert_to_xformer_model,
                                                         (actor, critic, initial_model, reward_model))

    actor_numel = get_model_numel(actor, strategy)
    critic_numel = get_model_numel(critic, strategy)
    initial_model_numel = get_model_numel(initial_model, strategy)
    reward_model_numel = get_model_numel(reward_model, strategy)
    print_model_numel({
        'Actor': actor_numel,
        'Critic': critic_numel,
        'Initial model': initial_model_numel,
        'Reward model': reward_model_numel
    })
    performance_evaluator = PerformanceEvaluator(actor_numel,
                                                 critic_numel,
                                                 initial_model_numel,
                                                 reward_model_numel,
                                                 enable_grad_checkpoint=False,
                                                 ignore_episodes=1)

    actor_optim = HybridAdam(actor.parameters(), lr=5e-6)
    critic_optim = HybridAdam(critic.parameters(), lr=5e-6)

    tokenizer = AutoTokenizer.from_pretrained('facebook/opt-350m')
    tokenizer.pad_token = tokenizer.eos_token

    with low_precision_init():
        (actor, actor_optim), (critic, critic_optim), initial_model, reward_model = strategy.prepare(
            (actor, actor_optim), (critic, critic_optim), initial_model, reward_model)

    print_rank_0(f'Mem after prepare: {psutil.Process(os.getpid()).memory_full_info().rss /1024**3:.2f} GB')
    # TODO(ver217): load checkpoint here

    trainer = PPOTrainer(strategy,
                         actor,
                         critic,
                         reward_model,
                         initial_model,
                         actor_optim,
                         critic_optim,
                         ptx_coef=args.ptx_coef,
                         max_epochs=args.max_epochs,
                         train_batch_size=args.train_batch_size,
                         offload_inference_models=args.offload_inference_models,
                         max_length=512,
                         do_sample=True,
                         temperature=1.0,
                         top_k=50,
                         use_cache=True,
                         pad_token_id=tokenizer.pad_token_id,
                         eos_token_id=tokenizer.eos_token_id,
                         callbacks=[performance_evaluator])

    random_prompts = torch.randint(tokenizer.vocab_size, (1000, 256), device=torch.cuda.current_device())
    ptx_prompts = torch.randint(tokenizer.vocab_size, (1000, 512), device=torch.cuda.current_device())
    dataloader = DataLoader(random_prompts,
                            batch_size=args.experience_batch_size,
                            shuffle=True,
                            collate_fn=preprocess_batch)
    ptx_dataloader = DataLoader(ptx_prompts,
                                batch_size=args.train_batch_size,
                                shuffle=True,
                                collate_fn=preprocess_ptx_batch)

    trainer.fit(dataloader,
                ptx_dataloader,
                num_episodes=args.num_episodes,
                max_timesteps=args.update_timesteps,
                update_timesteps=args.update_timesteps)

    print_rank_0(f'Peak CUDA mem: {torch.cuda.max_memory_allocated()/1024**3:.2f} GB')
    print_rank_0(f'Peak Mem: {get_max_memory()/1024**2:.2f} GB')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-m', '--model', default='350m')
    parser.add_argument('-c', '--critic_model', default='350m')
    parser.add_argument('-s',
                        '--strategy',
                        choices=[
                            'colossalai_gemini',
                            'colossalai_gemini_reshard',
                            'colossalai_gemini_cpu',
                            'tp_zero2',
                            'tp_zero2_cpu',
                        ],
                        default='colossalai_gemini_reshard')
    parser.add_argument('-t', '--tp_size', type=int, default=1)
    parser.add_argument('-e', '--num_episodes', type=int, default=3)
    parser.add_argument('-u', '--update_timesteps', type=int, default=1)
    parser.add_argument('--max_epochs', type=int, default=1)
    parser.add_argument('--train_batch_size', type=int, default=8)
    parser.add_argument('--experience_batch_size', type=int, default=8)
    parser.add_argument('-l', '--lora_rank', type=int, default=0)
    parser.add_argument('--cuda_mem_frac', type=float, default=1.0)
    parser.add_argument('-o', '--offload_inference_models', action='store_true', default=False)
    parser.add_argument('-k',
                        '--use_kernels',
                        action='store_true',
                        default=False,
                        help='This uses xformers kernels, which can save memory and accelerate training.')
    parser.add_argument('-g',
                        '--grad_checkpoint',
                        default=False,
                        action='store_true',
                        help='This uses gradient checkpointing, which can save memory and slow down training.')
    parser.add_argument('-p', '--ptx_coef', type=float, default=0.0)
    args = parser.parse_args()
    main(args)
