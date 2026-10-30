"""Settings of the image-generation pipeline. They are agreed decisions: change them through
decisions/ (tools/ledger.py propose, then the owner approves), never here alone."""

SAMPLER = "dpmpp_2m_sde"
STEPS = 28
LORAS = ["skin-detail-v3", "film-grain-v1"]
