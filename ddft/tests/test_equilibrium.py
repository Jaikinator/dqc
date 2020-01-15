import time
import random
import torch
from ddft.utils.fd import finite_differences
from ddft.tests.utils import compare_grad_with_fd
from ddft.modules.equilibrium import EquilibriumModule

def test_equil_1():
    class DummyModule(torch.nn.Module):
        def __init__(self, A):
            super(DummyModule, self).__init__()
            self.A = torch.nn.Parameter(A)

        def forward(self, y, x):
            # y: (nbatch, nr)
            # x: (nbatch, nr)
            nbatch = y.shape[0]
            tanh = torch.nn.Tanh()
            A = self.A.unsqueeze(0).expand(nbatch, -1, -1)
            Ay = torch.bmm(A, y.unsqueeze(-1)).squeeze(-1)
            Ayx = Ay + x
            return tanh(0.1 * Ayx)

    torch.manual_seed(100)
    random.seed(100)

    dtype = torch.float64
    nr = 7
    nbatch = 1
    A  = torch.randn((nr, nr)).to(dtype)
    x  = torch.rand((nbatch, nr)).to(dtype).requires_grad_()
    y0 = torch.rand((nbatch, nr)).to(dtype).requires_grad_()

    model = DummyModule(A)
    eqmodel = EquilibriumModule(model)
    y = eqmodel(y0, x)

    print("Forward results:")
    print(y)
    print(model(y, x))
    print("    should be close to 1:")
    print(y / model(y, x))

    def getloss(A, x, y0, return_model=False):
        model = DummyModule(A)
        eqmodel = EquilibriumModule(model)
        y = eqmodel(y0, x)
        loss = (y*y).sum()
        if not return_model:
            return loss
        else:
            return loss, model

    compare_grad_with_fd(getloss, (A, x, y0), [1], eps=1e-5, rtol=1e-3)

    # check A_grad manually because it is a parameter
    # gradient with backprop
    loss, model = getloss(A, x, y0, return_model=True)
    x.grad.zero_()
    loss.backward()
    A_grad = list(model.parameters())[0].grad.data
    # gradient with fd
    A_fd = finite_differences(getloss, (A, x, y0), 0, eps=1e-5)
    ratio = A_grad / A_fd
    assert torch.allclose(ratio, torch.ones_like(ratio), rtol=3e-3)