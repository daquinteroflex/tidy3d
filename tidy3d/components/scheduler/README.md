# Multi-Solver Simulation Scheduler


### Development Caveats

Note that because this scheduler "composes" a set of multi-solver simulations from a `CouplingSpec` and integrated simulation definition, this code conceptually lies at a higher level than the building-blocks it uses.

It cannot be built-upon from the base-solver abstract classes, in order not to create circular relationships. 