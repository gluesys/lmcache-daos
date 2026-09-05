"""LMCache multiprocess (MP) mode support: a DAOS ``L2AdapterInterface``
implementation and a server entry point that registers it.

Importing this package does not import LMCache; ``lmcache_daos.mp.l2_adapter``
does, so import that (or run ``python -m lmcache_daos.mp.server``) inside the
serving environment.
"""
