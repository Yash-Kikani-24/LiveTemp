"""
strategies/ — drop-in strategy package.

Adding a strategy means dropping ONE file in this folder that defines a class
implementing the Strategy interface (see base.py). The Engine's Runner
auto-discovers every such file at startup — nothing else needs to change.
This mirrors the modular method-registry pattern of the existing bias engine
(webinfo.txt section 4A).
"""
