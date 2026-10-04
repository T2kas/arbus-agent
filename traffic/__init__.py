"""Eismo raundas — the vehicle-counting worker.

Records traffic-camera clips, counts vehicles that enter a calibrated zone in a
calibrated direction (YOLOX detection + ByteTrack + PolygonZone), burns the
overlay into an MP4, and gives an admin a local tool to check the counts, tune
the parameters and mark vehicles in frames for detector fine-tuning.

Kept apart from the `arbus` agent on purpose: its dependencies (OpenCV,
ONNX Runtime, supervision) are heavy and the agent's GitHub Actions never need
them. See traffic/README.md.
"""
