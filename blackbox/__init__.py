"""Agent Black Box - a flight recorder for enterprise agents.

Records what an agent saw, retrieved, called and concluded; replays a recorded
suite against a changed configuration; and reports whether the change actually
helped, including what it broke.

Nothing here needs a network connection. The shipped demo agents are
deterministic simulations so the harness itself can be tested and demonstrated
offline; an Azure OpenAI adapter is provided for real runs.
"""

__version__ = "0.1.0"
