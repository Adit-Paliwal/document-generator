# Expose root_agent at the package level so Google ADK can discover it via
# the standard search path: Data_Ingestion.agent.root_agent
from .agent import root_agent  # noqa: F401
