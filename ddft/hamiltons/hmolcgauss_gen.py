from abc import abstractmethod, abstractproperty
from functools import reduce
import torch
import numpy as np
import lintorch as lt

from ddft.hamiltons.base_hamilton_gen import BaseHamiltonGenerator
from ddft.utils.spharmonics import spharmonics
from ddft.utils.gamma import incgamma
from ddft.csrc import get_ecoeff, get_overlap_mat, get_kinetics_mat

class HamiltonMoleculeCGaussGenerator(BaseHamiltonGenerator):
    """
    HamiltonMoleculeCGaussGenerator represents the system of multiple atoms with
    all-electrons potential at the centre of coordinate. The chosen basis is
    contracted Cartesian Gaussian centered at given positions.
    As the basis depends on the position of each atom, the batch only works
    for molecules with the same positions.

    Arguments
    ---------
    * grid: BaseGrid
        The integration grid. It should be a spherical harmonics radial grid
        centered at the centre of coordinate.
    * ijks: torch.tensor int (nelmtstot, 3)
        The power of xyz in the basis.
    * alphas: torch.tensor (nelmtstot,)
        The Gaussian exponent of the contracted Gaussians.
    * centres: torch.tensor (nelmtstot, 3)
        The position in Cartesian coordinate of the contracted Gaussians.
    * coeffs: torch.tensor (nelmtstot,)
        The contracted coefficients of each elements in the contracted Gaussians.
    * nelmts: int or torch.tensor int (nbasis,)
        The number of elements per basis. If it is an int, then the number of
        elements are the same for all bases. If it is a tensor, then the number
        of elements in each basis is indicated by the values in the tensor.
    * atompos: torch.tensor (natoms, 3)
        The position of the atoms (to be the central position of Coulomb potential).
    * atomzs: torch.tensor (natoms,)
        The atomic number of the atoms.
    * normalize_elmts: bool
        If True, then the basis is `sum(coeff * norm(alpha) * xyz * exp(-alpha*r^2))`.
        Otherwise, then the basis is `sum(coeff * xyz * exp(-alpha*r^2))`.

    Forward arguments
    -----------------
    * wf: torch.tensor (nbatch, nbasis, ncols)
        The basis coefficients of the wavefunction.
    * vext: torch.tensor (nbatch, nr)
        External potential other than the potential from the atoms.

    Overlap arguments
    -----------------
    * wf: torch.tensor (nbatch, nbasis, ncols)
        The basis coefficients of the wavefunction.
    """

    def __init__(self, grid,
                 ijks, alphas, centres, coeffs, nelmts,
                 atompos, atomzs, normalize_elmts=True):
        # self.nbasis, self.nelmts, ndim = centres.shape
        self.nelmtstot, ndim = centres.shape
        assert ndim == 3, "The centres must be 3 dimensions"
        self.nelmts = nelmts
        if isinstance(self.nelmts, torch.Tensor):
            self.nbasis = nelmts.shape[0]
        else:
            assert self.nelmtstot % self.nelmts == 0, "The number of gaussian is not the multiple of nelmts"
            self.nbasis = self.nelmtstot // self.nelmts
        self.natoms = atompos.shape[0]
        self._grid = grid
        dtype = alphas.dtype
        device = alphas.device
        super(HamiltonMoleculeCGaussGenerator, self).__init__(
            shape = (self.nbasis, self.nbasis),
            dtype = dtype,
            device = device)

        # get the matrices before contraction
        ecoeff_obj = Ecoeff(ijks, centres, alphas, atompos)
        self.olp_elmts = ecoeff_obj.get_overlap() # (nelmtstot, nelmtstot)
        self.kin_elmts = ecoeff_obj.get_kinetics() # (nelmtstot, nelmtstot)
        self.coul_elmts = ecoeff_obj.get_coulomb() * atomzs.unsqueeze(-1).unsqueeze(-1) # (natoms, nelmtstot, nelmtstot)

        # normalize each gaussian elements
        if normalize_elmts:
            norm_elmt = 1./torch.sqrt(torch.diagonal(self.olp_elmts)) # (nelmtstot,)
            coeffs = coeffs * norm_elmt

        # combine the kinetics and coulomb elements
        self.kin_coul_elmts = self.kin_elmts + self.coul_elmts.sum(dim=0) # (nelmtstot, nelmtstot)

        # prepare the contracted coefficients and indices
        self.coeffs2 = coeffs * coeffs.unsqueeze(1) # (nelmtstot, nelmtstot)
        if isinstance(self.nelmts, torch.Tensor):
            self.csnelmts = torch.cumsum(self.nelmts, dim=0)-1

        # get the contracted part
        self.kin_coul_mat = self._contract(self.kin_coul_elmts) # (nbasis, nbasis)
        self.olp_mat = self._contract(self.olp_elmts) # (nbasis, nbasis)

        # get the normalization constant
        norm = 1. / torch.sqrt(self.olp_mat.diagonal(dim1=-2, dim2=-1)) # (nbasis)
        norm_mat = norm.unsqueeze(-1) * norm.unsqueeze(1) # (nbasis, nbasis)

        # normalize the contracted matrix
        self.kin_coul_mat = self.kin_coul_mat * norm_mat
        self.olp_mat = self.olp_mat * norm_mat

        # make sure the matrix is symmetric
        self.kin_coul_mat = (self.kin_coul_mat + self.kin_coul_mat.transpose(-2,-1)) * 0.5
        self.olp_mat = (self.olp_mat + self.olp_mat.transpose(-2,-1)) * 0.5

        # get the basis
        self.rgrid = self._grid.rgrid_in_xyz # (nr, 3)
        rab = self.rgrid - centres.unsqueeze(1) # (nbasis*nelmts, nr, 3)
        dist_sq = (rab*rab).sum(dim=-1) # (nbasis*nelmts, nr)
        rab_power = ((rab+1e-15)**ijks.unsqueeze(1)).prod(dim=-1) # (nbasis*nelmts, nr)
        basis_all = rab_power * torch.exp(-alphas.unsqueeze(-1) * dist_sq) # (nbasis*nelmts, nr)
        basis_all_coeff = basis_all * coeffs.unsqueeze(-1) # (nelmtstot, nr)
        # contract the basis
        if isinstance(self.nelmts, torch.Tensor):
            basis = basis_all_coeff.cumsum(dim=0)[self.csnelmts,:] # (nbasis, nr)
            basis = torch.cat((basis[:1,:], basis[1:,:]-basis[:-1,:]), dim=0) # (nbasis, nr)
        else:
            basis = basis_all_coeff.view(self.nbasis, self.nelmts, -1).sum(dim=1) # (nbasis, nr)
        norm_basis = basis * norm.squeeze(0).unsqueeze(-1)
        self.basis = norm_basis # (nbasis, nr)
        self.basis_dvolume = self.basis * self._grid.get_dvolume() # (nbasis, nr)

    def get_hamiltonian(self, vext):
        # kin_coul_mat: (nbasis, nbasis)
        # vext: (..., nr)
        extpot_mat = torch.einsum("...r,br,cr->...bc", vext, self.basis_dvolume, self.basis)
        mat = extpot_mat + self.kin_coul_mat
        mat = (mat + mat.transpose(-2,-1)) * 0.5 # ensure the symmetricity
        return lt.LinearOperator.m(mat, is_hermitian=True)

    def get_overlap(self):
        return lt.LinearOperator.m(self.olp_mat, is_hermitian=True)

    def dm2dens(self, dm):
        # dm: (..., nbasis, nbasis)
        # self.basis: (nbasis, nr)
        # return: (..., nr)
        batchshape = dm.shape[:-2]
        # dm = dm.view(-1, *dm.shape[-2:]) # (nbatch, nbasis, nbasis)
        dens = torch.einsum("...jk,jr,kr->...r", dm, self.basis, self.basis) # (nbatch, nr)
        return dens#.view(*batchshape, -1) # (..., nr)

    def getparamnames(self, methodname, prefix=""):
        if methodname == "get_hamiltonian":
            return [prefix+"basis_dvolume", prefix+"basis", prefix+"kin_coul_mat"]
        elif methodname == "get_overlap":
            return [prefix+"olp_mat"]
        elif methodname == "dm2dens":
            return [prefix+"basis"]
        else:
            raise KeyError("getparamnames has no %s method" % methodname)

    ############################# helper functions #############################
    def _contract(self, mat):
        # multiply the matrix with the contracted coefficients
        mat = mat * self.coeffs2

        # resize mat to have shape of (-1, nbasis, nelmts, nbasis, nelmts)
        batch_size = mat.shape[:-2]
        mat_size = mat.shape[-2:]
        if isinstance(self.nelmts, torch.Tensor):
            mat = mat.view(-1, *mat_size) # (-1, nelmtstot, nelmtstot)
            csmat1 = torch.cumsum(mat, dim=1)[:,self.csnelmts,:] # (-1, nbasis, nelmtstot)
            mat1 = torch.cat((csmat1[:,:1,:], csmat1[:,1:,:]-csmat1[:,:-1,:]), dim=1) # (-1, nbasis, nelmtstot)
            csmat2 = torch.cumsum(mat1, dim=2)[:,:,self.csnelmts] # (-1, nbasis, nbasis)
            cmat = torch.cat((csmat2[:,:,:1], csmat2[:,:,1:]-csmat2[:,:,:-1]), dim=2) # (-1, nbasis, nbasis)
        else:
            mat = mat.view(-1, self.nbasis, self.nelmts, self.nbasis, self.nelmts)

            # sum the nelmts to get the basis
            cmat = mat.sum(dim=-1).sum(dim=-2) # (-1, nbasis, nbasis)
        cmat = cmat.view(*batch_size, self.nbasis, self.nbasis)
        return cmat

class Ecoeff(object):
    # ref: https://joshuagoings.com/2017/04/28/integrals/ and
    # Helgaker, Trygve, and Peter R. Taylor. “Gaussian basis sets and molecular integrals.” Modern Electronic Structure (1995).
    def __init__(self, ijks, centres, alphas, atompos):
        # ijks: (nbasis*nelmts, 3)
        # centres: (nbasis*nelmts, 3)
        # alphas: (nbasis*nelmts)
        # atompos: (natoms, 3)
        self.max_basis = 8
        self.max_ijkflat = 64
        ijk_left = ijks.unsqueeze(0) # (1, nbasis*nelmts, 3)
        ijk_right = ijks.unsqueeze(1) # (nbasis*nelmts, 1, 3)
        self.ndim = ijk_left.shape[-1]
        ijk_pairs = ijk_left * self.max_basis + ijk_right # (nbasis*nelmts, nbasis*nelmts, 3)
        ijk_pairs2 = ijk_pairs[:,:,0] * self.max_ijkflat*self.max_ijkflat + \
                     ijk_pairs[:,:,1] * self.max_ijkflat + ijk_pairs[:,:,2] # (nbasis*nelmts, nbasis*nelmts)
        qab = centres - centres.unsqueeze(1) # (nbasis*nelmts, nbasis*nelmts, 3)
        qab_sq = qab**2 # (nbasis*nelmts, nbasis*nelmts, 3)
        gamma = (alphas + alphas.unsqueeze(1)) # (nbasis*nelmts, nbasis*nelmts)
        kappa = (alphas * alphas.unsqueeze(1)) / gamma # (nbasis*nelmts, nbasis*nelmts)
        mab = torch.exp(-kappa.unsqueeze(-1) * qab_sq) # (nbasis*nelmts, nbasis*nelmts, 3)
        ra = alphas.unsqueeze(-1) * centres # (nbasis*nelmts, 3)
        rc = (ra + ra.unsqueeze(1)) / gamma.unsqueeze(-1) # (nbasis*nelmts, nbasis*nelmts, 3)
        rcd = rc - atompos.unsqueeze(1).unsqueeze(1) # (natoms, nbasis*nelmts, nbasis*neltms, 3)
        rcd_sq = (rcd*rcd).sum(dim=-1) # (natoms, nbasis*nelmts, nbasis*neltms)

        self.ijk_left = ijk_left
        self.ijk_right = ijk_right
        self.ijk_pairs = ijk_pairs
        self.ijk_pairs2 = ijk_pairs2
        self.alphas = alphas.unsqueeze(0)
        self.betas = alphas.unsqueeze(1)
        self.qab = qab
        self.qab_sq = qab_sq
        self.gamma = gamma
        self.kappa = kappa
        self.mab = mab
        self.ra = ra
        self.rc = rc
        self.rcd = rcd
        self.rcd_sq = rcd_sq

        self.ijk_left_max = self.ijk_left.max()
        self.ijk_right_max = self.ijk_right.max()
        self.ijk_pairs2_unique = torch.unique(self.ijk_pairs2)
        self.idx_ijk = [(self.ijk_pairs2 == ijk_flat_value) for ijk_flat_value in self.ijk_pairs2_unique]

        # the key is: "i,j,t,xyz"
        self.key_format = "{},{},{},{}"
        # the value's shape is: (nbasis*nelmts, nbasis*nelmts)
        self.e_memory = {
            "0,0,0,0": self.mab[:,:,0],
            "0,0,0,1": self.mab[:,:,1],
            "0,0,0,2": self.mab[:,:,2],
        }
        # the key format is: r,s,t,n
        # the value's shape is: (natoms, nbasis*nelmts, nbasis*nelmts)
        self.r_memory = {}

        # cache
        self.overlap = None
        self.kinetics = None
        self.coulomb = None

    def _split_pair(self, ijkpair):
        return ijkpair // self.max_basis, ijk_pair % self.max_basis

    def get_overlap(self):
        if self.overlap is None:
            # NOTE: using this makes test_grad_intermediate.py::test_grad_dft_cgto produces nan in gradgradcheck energy
            # (nans are in the numerical, not in the analytic one)
            self.overlap = get_overlap_mat(self.ijk_left_max, self.ijk_right_max,
                self.max_basis, self.ndim, self.ijk_pairs,
                self.alphas, self.betas, self.gamma, self.kappa, self.qab,
                self.e_memory, self.key_format)
        return self.overlap

    def get_kinetics(self):
        if self.kinetics is None:
            self.kinetics = get_kinetics_mat(self.ijk_left_max, self.ijk_right_max,
                self.max_basis, self.ndim, self.ijk_pairs,
                self.alphas, self.betas, self.gamma, self.kappa, self.qab,
                self.e_memory, self.key_format)
        return self.kinetics

    def get_coulomb(self):
        # returns (natoms, nbasis*nelmts, nbasis*nelmts)
        if self.coulomb is None:
            # coulomb: (natoms, nbasis*nelmts, nbasis*nelmts)
            coulomb = torch.zeros_like(self.rcd_sq).to(self.rcd_sq.device)
            for ijk_flat_value, idx in zip(self.ijk_pairs2_unique, self.idx_ijk):
                # idx: (nbasis*nelmts, nbasis*nelmts)
                k, l, m, u, v, w = self._unpack_ijk_flat_value(ijk_flat_value)

                for r in range(k+u+1):
                    Erku = self.get_coeff(k, u, r, 0)[idx] # flatten tensor
                    for s in range(l+v+1):
                        Eslv = self.get_coeff(l, v, s, 1)[idx]
                        for t in range(m+w+1):
                            Etmw = self.get_coeff(m, w, t, 2)[idx]
                            Rrst = self.get_rcoeff(r, s, t, 0)[:,idx] # (natoms, -1)
                            coulomb[:,idx] += (Erku * Eslv * Etmw) * Rrst
            self.coulomb = -(2*np.pi/self.gamma) * coulomb
        return self.coulomb

    def get_coeff(self, i, j, t, xyz):
        return get_ecoeff(i, j, t, xyz,
            self.alphas, self.betas, self.gamma, self.kappa, self.qab,
            self.e_memory, self.key_format)

    def get_rcoeff(self, r, s, t, n):
        # rcd: (natoms, nbasis*nelmts, nbasis*nelmts, 3)
        # return: (natoms, nbasis*nelmts, nbasis*nelmts)
        if r < 0 or s < 0 or t < 0:
            return 0.0
        coeff = self._access_rcoeff(r, s, t, n)
        if coeff is not None:
            return coeff

        if r == 0 and s == 0 and t == 0:
            # (natoms, nbasis*nelmts, nbasis*nelmts)
            coeff = (-2*self.gamma)**n * self._boys(n, self.gamma * self.rcd_sq)
        elif r > 0:
            coeff = (r-1) * self.get_rcoeff(r-2, s, t, n+1) + \
                    self.rcd[:,:,:,0] * self.get_rcoeff(r-1, s, t, n+1)
        elif s > 0:
            coeff = (s-1) * self.get_rcoeff(r, s-2, t, n+1) + \
                    self.rcd[:,:,:,1] * self.get_rcoeff(r, s-1, t, n+1)
        elif t > 0:
            coeff = (t-1) * self.get_rcoeff(r, s, t-2, n+1) + \
                    self.rcd[:,:,:,2] * self.get_rcoeff(r, s, t-1, n+1)

        # save the coefficients
        key = self.key_format.format(r,s,t,n)
        self.r_memory[key] = coeff
        return coeff

    def _unpack_ijk_flat_value(self, ijk_flat_value):
        ijk_pair2 = ijk_flat_value % self.max_ijkflat
        ijk_pair1 = (ijk_flat_value // self.max_ijkflat) % self.max_ijkflat
        ijk_pair0 = (ijk_flat_value // self.max_ijkflat) // self.max_ijkflat
        k = ijk_pair0 // self.max_basis
        l = ijk_pair1 // self.max_basis
        m = ijk_pair2 // self.max_basis
        u = ijk_pair0 % self.max_basis
        v = ijk_pair1 % self.max_basis
        w = ijk_pair2 % self.max_basis
        return k, l, m, u, v, w

    def _boys(self, n, T):
        nhalf = n + 0.5
        T = T + 1e-12 # add small noise
        # return incgamma(nhalf, T) / 2 * T**(1-nhalf)
        return incgamma(nhalf, T) / (2 * T**nhalf)

    def _access_coeff(self, i, j, t, xyz):
        key = self.key_format.format(i,j,t,xyz)
        if key in self.e_memory:
            return self.e_memory[key]
        else:
            return None

    def _access_rcoeff(self, r, s, t, n):
        key = self.key_format.format(r,s,t,n)
        if key in self.r_memory:
            return self.r_memory[key]
        else:
            return None