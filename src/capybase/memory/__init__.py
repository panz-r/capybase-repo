"""The learning flywheel: experience store + retrieval for RAG few-shot.

The journal records every resolution attempt; the memory layer distills
accepted ones into a labeled corpus of HistoricalExample records and retrieves
the most similar past merges for new conflicts, injecting them into the prompt
as dynamic few-shot demonstrations. This is the seam that lets capybase
improve with use without retraining the model.
"""
