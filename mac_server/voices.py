"""The Gemini Live voice catalogue: name -> (gender, French descriptor), and where its samples live.

Descriptors come from Google's Live/TTS voice table (original in the trailing comment), genders from
the Cloud TTS voice directory. 14 female, 16 male.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, Literal

Gender = Literal['female', 'male']

SAMPLES_DIR: Final = Path(__file__).resolve().parent / 'assets' / 'voice_samples'

VOICES: Final[dict[str, tuple[Gender, str]]] = {
    'Achernar': ('female', 'douce'),  # Soft
    'Achird': ('male', 'amicale'),  # Friendly
    'Algenib': ('male', 'éraillée'),  # Gravelly
    'Algieba': ('male', 'fluide'),  # Smooth
    'Alnilam': ('male', 'ferme'),  # Firm
    'Aoede': ('female', 'légère'),  # Breezy
    'Autonoe': ('female', 'lumineuse'),  # Bright
    'Callirrhoe': ('female', 'décontractée'),  # Easy-going
    'Charon': ('male', 'informative'),  # Informative
    'Despina': ('female', 'fluide'),  # Smooth
    'Enceladus': ('male', 'voilée'),  # Breathy
    'Erinome': ('female', 'claire'),  # Clear
    'Fenrir': ('male', 'exaltée'),  # Excitable
    'Gacrux': ('female', 'mûre'),  # Mature
    'Iapetus': ('male', 'claire'),  # Clear
    'Kore': ('female', 'ferme'),  # Firm
    'Laomedeia': ('female', 'enjouée'),  # Upbeat
    'Leda': ('female', 'jeune'),  # Youthful
    'Orus': ('male', 'ferme'),  # Firm
    'Puck': ('male', 'enjouée'),  # Upbeat
    'Pulcherrima': ('female', 'directe'),  # Forward
    'Rasalgethi': ('male', 'informative'),  # Informative
    'Sadachbia': ('male', 'vive'),  # Lively
    'Sadaltager': ('male', 'savante'),  # Knowledgeable
    'Schedar': ('male', 'posée'),  # Even
    'Sulafat': ('female', 'chaleureuse'),  # Warm
    'Umbriel': ('male', 'décontractée'),  # Easy-going
    'Vindemiatrix': ('female', 'délicate'),  # Gentle
    'Zephyr': ('female', 'lumineuse'),  # Bright
    'Zubenelgenubi': ('male', 'familière'),  # Casual
}

DEFAULT_VOICE: Final = 'Achird'
