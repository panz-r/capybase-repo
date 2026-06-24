"""capybase — a rebase-conflict resolution agent with research-grade seams.

The MVP auto-resolves ordinary UTF-8 text-file `UU` (both-modified) git
conflicts using a single local OpenAI-compatible language model. Every
interface is designed so structural merge, RAG, verifier models, and
calibrated risk can be added later without rewriting the orchestrator.

Core invariant::

    A ConflictUnit becomes one or more CandidateResolutions;
    validators produce VerificationResults;
    risk policy chooses accept/retry/escalate;
    only the orchestrator mutates Git.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
