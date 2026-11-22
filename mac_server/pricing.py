"""Gemini Live prices and the cost of one `usage` event (each usage_metadata is billed on its own)."""

from __future__ import annotations

from typing import Final

from mac_server.contracts import Usage

MODEL: Final = 'gemini-3.8-live'
SOURCE: Final = 'https://ai.google.dev/gemini-api/docs/pricing'
CHECKED: Final = '2026-10-30'

# USD per 1M tokens, paid tier.
INPUT_USD_PER_M: Final = {'text': 0.75, 'audio': 3.00, 'image': 1.00}  # image: images and video frames
OUTPUT_USD_PER_M: Final = {'text': 4.50, 'audio': 12.00}  # text includes thinking


def cost_usd(usage: Usage) -> float:
    prompt, response = usage.prompt, usage.response
    usd = prompt.text * INPUT_USD_PER_M['text'] + prompt.audio * INPUT_USD_PER_M['audio']
    usd += prompt.image * INPUT_USD_PER_M['image']
    # Live has no image output; were a count ever reported, the closest output rate is text's.
    usd += (response.text + response.image) * OUTPUT_USD_PER_M['text'] + response.audio * OUTPUT_USD_PER_M['audio']
    return usd / 1_000_000
