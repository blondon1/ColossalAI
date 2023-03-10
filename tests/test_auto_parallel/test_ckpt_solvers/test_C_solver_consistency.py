import copy

import pytest
import torch
import torch.fx
import torch.multiprocessing as mp
import torchvision.models as tm

import colossalai
from colossalai.core import global_context as gpc
from colossalai.fx import ColoGraphModule, ColoTracer
from colossalai.fx._compatibility import is_compatible_with_meta
# from colossalai.fx.passes.algorithms import solver_rotor
# from colossalai.fx.passes.algorithms.operation import Sequence
from colossalai.fx.passes.meta_info_prop import MetaInfoProp
from colossalai.utils import free_port

if is_compatible_with_meta():
    from colossalai.fx.profiler.tensor import MetaTensor

try:
    from colossalai.fx.codegen import ActivationCheckpointCodeGen
    withcodegen = True
except:
    from colossalai.fx.codegen import python_code_with_activation_checkpoint
    withcodegen = False


def _run_C_solver_consistency_test(rank=0):
    colossalai.launch(config={}, rank=rank, world_size=1, host='localhost', port=free_port(), backend='nccl')

    for M, mem_budget in [(tm.resnet50, 4000), (tm.densenet121, 8080)]:
        model = M()
        data = torch.rand(128, 3, 224, 224, device='meta')

        tracer = ColoTracer()
        graph = tracer.trace(model, meta_args={"x": data})
        graph.set_codegen(ActivationCheckpointCodeGen())
        gm = ColoGraphModule(model, graph, model.__class__.__name__)
        if is_compatible_with_meta():
            data_meta = MetaTensor(data, fake_device=next(gm.parameters()).device)
        MetaInfoProp(gm).run(data_meta)

        # python solver
        gm = solver_rotor(gm, data_meta, mem_budget * 1024 * 1024, force_python=True)
        sequence_python: Sequence = copy.deepcopy(gm.__sequence__)
        opt_python = copy.deepcopy(gm.__opttable__)

        # C solver
        gm = solver_rotor(gm, data_meta, mem_budget * 1024 * 1024)
        sequence_C: Sequence = copy.deepcopy(gm.__sequence__)
        opt_C = copy.deepcopy(gm.__opttable__)

        # make sure the opt_tables are the same
        for m in range(len(opt_python)):
            for d in range(1, len(opt_python[0])):
                for i in range(len(opt_python[0]) - d):
                    assert opt_python[m][i][i + d] == opt_C[m][i][i + d], \
                    f"item ({m}, {i}, {i + d}) is not consistent with python version!\npython version: {opt_python[m][i][i + d]}\nC version: {opt_C[m][i][i + d]}"

        sequence_python = sequence_python.list_operations()
        sequence_C = sequence_C.list_operations()

        # make sure the sequences are the same
        assert len(sequence_python) == len(sequence_C) and \
        all(python_op.__repr__() == C_op.__repr__() for (python_op, C_op) in zip(sequence_python, sequence_C))

    gpc.destroy()


@pytest.mark.skip("TODO(lyl): refactor all tests.")
@pytest.mark.skipif(not withcodegen, reason="torch version is less than 1.12.0")
def test_C_solver_consistency():
    mp.spawn(_run_C_solver_consistency_test, nprocs=1)


if __name__ == '__main__':
    _run_C_solver_consistency_test(rank=0)