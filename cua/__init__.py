"""
cua - a computer-use agent for legacy applications without an API.

The model discovers a task once on masked screens; the flow becomes a typed, versioned capability;
deterministic replay (no model) is how an AI agent invokes it; a person can take over the live
session at any time; every run feeds a reliability signal back into the map of the app.

    cua.engine       discovery, replay, hand-over, runner (goal / capability -> RunResult)
    cua.artifact     capability schema, output extraction, outcome taxonomy + result contract
    cua.perception   the Surface seam: screenshot + OCR + OpenCV parsing, mouse / keyboard
    cua.safety       allow-list policy, PII vault, input guard
    cua.operator     operator panel (Agent/Human switch), recorder of a person's actions
    cua.learning     map of the app's windows, rewards and reliability (learning from outcomes)
"""
__version__ = "1.1.0"
