"""change-windows core: rules, window store, guard decision, agent tools, initData check.

Pure Python (stdlib, plus ``cryptography`` for the Ed25519 initData check) so the test suite runs
without Hermes. The Hermes wiring lives in the plugin's top-level ``__init__.py``.
"""

__version__ = "0.1.0"
