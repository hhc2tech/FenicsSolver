from __future__ import print_function, division
import math
import collections
import numbers
import numpy as np


#####################################
from dolfin import *
from mshr import Box

# Test for PETSc
if not has_linear_algebra_backend("PETSc"):
    print("DOLFIN has not been configured with PETSc. Exiting.")
    exit()
# Set backend to PETSC
parameters["linear_algebra_backend"] = "PETSc"


from SolverBase import SolverBase, SolverError
class LinearElasticitySolver(SolverBase):
    """ transient and  thermal stress is not implemented
    complex boundary, pressure, force, displacement
    """
    def __init__(self, case_settings):
        SolverBase.__init__(self, case_settings)

        # there must be a value for body force as source item, to make L not empyt in a == L
        if self.body_source:
            self.body_source = self.translate_value(self.body_source)
        else:
            if self.dimension == 3:
                self.body_source = Constant((0, 0, 0))
            else:
                self.body_source = Constant((0, 0))

        # thermal stress, material 
        if 'temperature_distribution' in case_settings:
            self.thermal_stress = True
            self.temperature_distribution = case_settings['temperature_distribution']

        self.solving_modal = False

    def set_function_space(self, mesh_or_function_space, periodic_boundary):
        # coupled NS fucntion using mixed function space
        #print("mesh_or_function_space", type(mesh_or_function_space))
        try:
            self.mesh = mesh_or_function_space
            if periodic_boundary:
                self.function_space = VectorFunctionSpace(self.mesh, "CG", 1, constrained_domain=periodic_boundary)
                # the group and degree of the FE element.
            else:
                self.function_space = VectorFunctionSpace(self.mesh, "CG", 1)
        except:
            self.function_space = mesh_or_function_space
            self.mesh = self.function_space.mesh()
        self.is_mixed_function_space = False  # how to detect it is mixed, vector, scaler , tensor?

    def get_internal_field(self):
        if not self.initial_values:
            if self.dimension == 3:
                self.initial_values = Constant((0, 0, 0))
            else:
                self.initial_values = Constant((0, 0))
        v0 = self.initial_values
        if isinstance(v0, (Constant, Expression)):
            d0 = interpolate(v0, self.function_space)
        return d0

    # Stress computation
    def sigma(self, u):
        elasticity = self.material['elastic_modulus']
        nu = self.material['poisson_ratio']
        mu = elasticity/(2.0*(1.0 + nu))
        lmbda = elasticity*nu/((1.0 + nu)*(1.0 - 2.0*nu))
        return 2.0*mu*sym(grad(u)) + lmbda*tr(sym(grad(u)))*Identity(len(u))

    # Strain energy 
    def energy(self, u):
        elasticity = self.material['elastic_modulus']
        nu = self.material['poisson_ratio']
        mu = elasticity/(2.0*(1.0 + nu))
        lmbda = elasticity*nu/((1.0 + nu)*(1.0 - 2.0*nu))
        return lmbda/2.0*(tr(eps(v)))^2 + mu*tr(eps(v)**2)

    def von_Mises(self, u):
        s = self.sigma(u) - (1./3)*tr(self.sigma(u))*Identity(self.dimension)  # deviatoric stress
        von_Mises = sqrt(3./2*inner(s, s))
        
        V = FunctionSpace(self.mesh, 'P', 1)  # correct, but why using another function space
        return project(von_Mises, V)

    def update_boundary_conditions(self, time_iter_, u_0, u_1):
        V = self.function_space
        # Define variational problem
        u = TrialFunction(V)
        v = TestFunction(V)

        elasticity = self.material['elastic_modulus']
        nu = self.material['poisson_ratio']
        mu = elasticity/(2.0*(1.0 + nu))
        lmbda = elasticity*nu/((1.0 + nu)*(1.0 - 2.0*nu))

        a = inner(self.sigma(u), grad(v))*dx
        integrals_F = []
        if self.body_source:
            integrals_F.append( inner(self.body_source, v)*dx )

        ## thermal stress
        if self.temperature_distribution:
            T = self.translate_value(self.temperature_distribution)
            thermal_stress = inner(elasticity * grad( T - Constant(self.reference_values['temperature'])) , v)*dx
            integrals_F.append( thermal_stress )

        ## boundary setup
        boundary_facets = FacetFunction('size_t', self.mesh)
        boundary_facets.set_all(0)
        ## surface boundary conditions applying
        for name, bc in self.boundary_conditions.items():
            bc['boundary'].mark(boundary_facets, bc['boundary_id'])

        ds= Measure("ds", subdomain_data=boundary_facets)  # if later marking updating in this ds?
        if time_iter_==0:
            plot(boundary_facets, title = "boundary facets colored by ID")

        bcs = []
        mesh_normal = FacetNormal(self.mesh)  # n is predefined as normal?
        for name, bc in self.boundary_conditions.items():
            i = bc['boundary_id']
            if bc['type'] =='Dirichlet' or bc['type'] =='displacement':
                if isinstance(bc['value'], (Expression, Constant)):
                    dbc = DirichletBC(V, bc['value'], boundary_facets, i)
                    bcs.append(dbc)
                else: # transient setting from time_stamp
                    axis_i=0
                    for disp in bc['value']:
                        if disp:
                            dbc = DirichletBC(V.sub(axis_i), Constant(disp), boundary_facets, i)
                            bcs.append(dbc)
                        axis_i += 1
            elif bc['type'] == 'force':
                bc_force = bc['value']
                # calc the surface area and calc stress, normal and tangential?
                bc_area = assemble(Constant(1)*ds(bc['boundary_id'], domain=self.mesh))
                print('boundary area (m2) for force boundary is', bc_area)
                g = bc_force / bc_area
                if 'direction' in bc and bc['direction']:
                    direction_vector = bc['direction']
                else:
                    direction_vector = mesh_normal
                integrals_F.append( dot(g,v)*ds(i))
            elif bc['type'] == 'stress' or bc['type'] =='Neumann':
                if 'direction' in bc and bc['direction']:
                    direction_vector = bc['direction']
                else:
                    direction_vector = mesh_normal  # normal to boundary surface, n is predefined
                g = bc['value']
                integrals_F.append(dot(g,v)*ds(i))
            elif bc['type'] == 'symmetry':
                raise SolverError('thermal boundary type`{}` is not supported'.format(bc['type']))
            else:
                raise SolverError('thermal boundary type`{}` is not supported'.format(bc['type']))
        ## nodal constraint is not yet supported, try make it a small surface load instead

        # Assemble system, applying boundary conditions and extra items
        F = a
        if len(integrals_F):
            for item in integrals_F: F += item  # L side

        return F, bcs

    def solve(self):
        u = self.solve_transient()
        
        if self. solving_modal:
            self.solve_modal(F, bcs)  # test passed

        return u

    def solve_modal(self, F, bcs):
        # Assemble stiffness form, it is not fully tested yet
        A = PETScMatrix()
        b = PETScVector()
        '''
        assemble(a, tensor=A)
        for bc in bcs:
            bc.apply(A)          # apply the boundary conditions
        '''
        assemble_system(lhs(F), rhs(F), bcs, A_tensor=A, b_tensor=b)  # preserve symmetry

        # Create eigensolver
        eigensolver = SLEPcEigenSolver(A)

        # Compute all eigenvalues of A x = \lambda x
        print("Computing eigenvalues. This can take a minute.")
        eigensolver.solve()

        # Extract largest (first) eigenpair
        r, c, rx, cx = eigensolver.get_eigenpair(0)

        print("Largest eigenvalue: ", r)

        # Initialize function and assign eigenvector
        ev = Function(self.function_space)
        ev.vector()[:] = rx

        return ev

    def solve_static(self, F, u_0=None, bcs = []):
        # Create solution function, why not init this function?
        if u_0:
            u = u_0
        else:
            u = Function(self.function_space)

        #if self.is_iterative_solver:
        #u = self.solve_iteratively(F, bcs, u)
        u = self.solve_amg(F, bcs, u)
        # calc boundingbox to make sure no large deformation?
        return u


if __name__ == '__main__':
    test()