"""Judge every song as an individual, by how it sounds.

Community tags mix four kinds of words:

- **vibe**: what the song sounds or feels like: chill, mellow, funky, groovy, upbeat, dreamy,
  melancholic, energetic, instrumental, piano, female vocals, late night, summer ...
- **genre**: scene labels that are usually inherited from the *artist*: rap, hip hop, rock,
  pop, metal, country, edm ... "Lavender Buds" is MF DOOM, so taggers write "hip hop", even
  though it sounds chill and jazzy.
- **era**: 80s, 90s, oldies ...
- **ignore**: places, nationalities, years, "cover", "live" ...

By default a song is judged only on its **own** vibe: tags on the song itself (Last.fm track,
MusicBrainz recording) plus AcousticBrainz's audio-derived measures. Artist-level tags are not
used, except as a clearly flagged last resort for songs with no song-level data at all.
Genre and era words count **only in playlists that ask for them** (a playlist whose
definition mentions "80s" or "rap"). Some genres also say something about the sound, so they
add a softer vibe word ("jazz" → jazzy, "funk" → funky, "metal" → heavy, "lo-fi" → lo-fi).

You can reclassify any tag (``curator tags set "indie" vibe``); your choice always wins.
Everything here is a fixed, readable rule set: no model, nothing learned from your library.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from playlist_curator.storage.database import Database, utcnow

CLASSES = ("vibe", "genre", "era", "ignore")

# Weights
SOUND_GENRE_WEIGHT = 0.6  # a genre's implied vibe counts less than a direct vibe word
# Unrecognized tags are ignored: on less-played songs they're mostly radio stations, playlist
# names and in-jokes ("wsum 91.7 fm madison"). Promote real ones with `curator tags set`.
OTHER_WEIGHT = 0.0
ARTIST_FALLBACK = 0.5  # only for songs with no song-level data at all

# Vibe words, with spelling variants folded together
VIBE_ALIASES: dict[str, str] = {
    "chillout": "chill",
    "chill out": "chill",
    "chilled": "chill",
    "chill vibes": "chill",
    "relaxing": "relaxed",
    "relax": "relaxed",
    "calming": "calm",
    "soothing": "calm",
    "peaceful": "calm",
    "tranquil": "calm",
    "melancholy": "melancholic",
    "melancholia": "melancholic",
    "sadness": "sad",
    "depressing": "sad",
    "feel good": "feel-good",
    "feelgood": "feel-good",
    "good vibes": "feel-good",
    "uplifting": "feel-good",
    "happy": "happy",
    "upbeat": "upbeat",
    "energetic": "energetic",
    "high energy": "energetic",
    "energy": "energetic",
    "hype": "hype",
    "pump up": "hype",
    "party": "party",
    "dance": "danceable",
    "dancing": "danceable",
    "danceable": "danceable",
    "groove": "groovy",
    "groovy": "groovy",
    "funky": "funky",
    "jazzy": "jazzy",
    "soulful": "soulful",
    "smooth": "smooth",
    "mellow": "mellow",
    "laid back": "laid-back",
    "laidback": "laid-back",
    "easy listening": "easy listening",
    "dreamy": "dreamy",
    "ethereal": "dreamy",
    "hazy": "dreamy",
    "atmospheric": "atmospheric",
    "atmosphere": "atmospheric",
    "spacey": "atmospheric",
    "space": "atmospheric",
    "moody": "moody",
    "dark": "dark",
    "haunting": "dark",
    "eerie": "dark",
    "aggressive": "aggressive",
    "angry": "aggressive",
    "intense": "intense",
    "epic": "epic",
    "cinematic": "cinematic",
    "beautiful": "beautiful",
    "emotional": "emotional",
    "introspective": "introspective",
    "nostalgic": "nostalgic",
    "nostalgia": "nostalgic",
    "romantic": "romantic",
    "love songs": "romantic",
    "love song": "romantic",
    "sexy": "sensual",
    "sensual": "sensual",
    "catchy": "catchy",
    "sing along": "sing-along",
    "singalong": "sing-along",
    "anthem": "anthemic",
    "anthemic": "anthemic",
    "quirky": "quirky",
    "weird": "quirky",
    "experimental": "experimental",
    "psychedelic": "psychedelic",
    "trippy": "psychedelic",
    "lo-fi": "lo-fi",
    "lush": "lush",
    "warm": "warm",
    "raw": "raw",
    "heavy": "heavy",
    "loud": "loud",
    "soft": "soft",
    "quiet": "soft",
    "gentle": "soft",
    "slow": "slow",
    "fast": "fast",
    "instrumental": "instrumental",
    "acoustic": "acoustic",
    "piano": "piano",
    "guitar": "guitar",
    "guitar solo": "guitar",
    "acoustic guitar": "acoustic",
    "drums": "drums",
    "bass": "bass",
    "bassline": "bass",
    "synth": "synth",
    "synths": "synth",
    "strings": "strings",
    "orchestral": "orchestral",
    "saxophone": "saxophone",
    "sax": "saxophone",
    "horns": "horns",
    "brass": "horns",
    "female vocalists": "female vocals",
    "female vocalist": "female vocals",
    "female vocals": "female vocals",
    "male vocalists": "male vocals",
    "male vocals": "male vocals",
    "beats": "beats",
    "summer": "summer",
    "beach": "summer",
    "sunny": "summer",
    "sunshine": "summer",
    "night": "late night",
    "late night": "late night",
    "nocturnal": "late night",
    "midnight": "late night",
    "driving": "driving",
    "road trip": "driving",
    "study": "focus",
    "studying": "focus",
    "focus": "focus",
    "concentration": "focus",
    "workout": "workout",
    "gym": "workout",
    "running": "workout",
    "sleep": "sleep",
    "rainy day": "rainy day",
    "rain": "rainy day",
    "morning": "morning",
    "sunset": "sunset",
    "cozy": "cozy",
    # --- feelings
    "bittersweet": "bittersweet",
    "wistful": "bittersweet",
    "poignant": "bittersweet",
    "hopeful": "hopeful",
    "optimistic": "hopeful",
    "triumphant": "triumphant",
    "victorious": "triumphant",
    "empowering": "empowering",
    "motivational": "empowering",
    "inspiring": "empowering",
    "confident": "confident",
    "swagger": "confident",
    "cocky": "confident",
    "sassy": "sassy",
    "playful": "playful",
    "silly": "playful",
    "funny": "playful",
    "humorous": "playful",
    "whimsical": "playful",
    "cheerful": "happy",
    "joyful": "happy",
    "euphoric": "euphoric",
    "blissful": "euphoric",
    "lonely": "lonely",
    "loneliness": "lonely",
    "heartbreak": "heartbreak",
    "breakup": "heartbreak",
    "break up": "heartbreak",
    "heartbroken": "heartbreak",
    "longing": "longing",
    "yearning": "longing",
    "angst": "angsty",
    "angsty": "angsty",
    "rebellious": "rebellious",
    "anxious": "tense",
    "tense": "tense",
    "suspenseful": "tense",
    "ominous": "dark",
    "creepy": "dark",
    "sinister": "dark",
    "spooky": "dark",
    "mysterious": "mysterious",
    "passionate": "passionate",
    "intimate": "intimate",
    "tender": "tender",
    "sentimental": "tender",
    "sweet": "tender",
    "serene": "calm",
    "meditative": "meditative",
    "meditation": "meditative",
    "hypnotic": "hypnotic",
    "trance like": "hypnotic",
    "contemplative": "introspective",
    "reflective": "introspective",
    "thoughtful": "introspective",
    "uplifting music": "feel-good",
    "fiery": "intense",
    "powerful": "powerful",
    "majestic": "epic",
    "grand": "epic",
    "dramatic": "dramatic",
    "theatrical": "dramatic",
    # --- energy & rhythm
    "chill beats": "chill",
    "vibey": "chill",
    "vibes": "chill",
    "lazy": "laid-back",
    "breezy": "breezy",
    "bouncy": "bouncy",
    "bounce": "bouncy",
    "punchy": "punchy",
    "driving beat": "driving",
    "pulsing": "pulsing",
    "throbbing": "pulsing",
    "headbanging": "headbang",
    "headbang": "headbang",
    "mosh": "headbang",
    "explosive": "explosive",
    "frantic": "frantic",
    "chaotic": "frantic",
    "slow burn": "slow burn",
    "build up": "builds up",
    "builds": "builds up",
    "crescendo": "builds up",
    "drop": "drops",
    "bass drop": "drops",
    "banger": "banger",
    "bangers": "banger",
    "bop": "banger",
    "jam": "jam",
    "jams": "jam",
    "foot tapping": "groovy",
    "rhythmic": "rhythmic",
    "percussive": "percussive",
    "swinging": "swinging",
    "shuffle": "swinging",
    "syncopated": "rhythmic",
    "mid tempo": "mid-tempo",
    "midtempo": "mid-tempo",
    "slow tempo": "slow",
    "fast paced": "fast",
    "uptempo": "upbeat",
    "up tempo": "upbeat",
    # --- texture & production
    "fuzzy": "fuzzy",
    "fuzz": "fuzzy",
    "distorted": "distorted",
    "distortion": "distorted",
    "gritty": "gritty",
    "grimy": "gritty",
    "crunchy": "gritty",
    "jangly": "jangly",
    "jangle": "jangly",
    "twangy": "twangy",
    "twang": "twangy",
    "reverb": "reverb",
    "reverb heavy": "reverb",
    "echo": "reverb",
    "spacious": "spacious",
    "airy": "spacious",
    "minimal": "minimal",
    "minimalist": "minimal",
    "sparse": "minimal",
    "stripped down": "minimal",
    "layered": "lush",
    "wall of sound": "lush",
    "polished": "polished",
    "glossy": "polished",
    "lo fi": "lo-fi",
    "lofi": "lo-fi",
    "vinyl": "lo-fi",
    "tape": "lo-fi",
    "analog": "warm",
    "crisp": "bright",
    "bright": "bright",
    "shimmering": "bright",
    "sparkly": "bright",
    "glittery": "bright",
    "woozy": "dreamy",
    "floaty": "dreamy",
    "underwater": "dreamy",
    "glitchy": "glitchy",
    "glitch": "glitchy",
    "noisy": "noisy",
    "noise": "noisy",
    "groovy bassline": "bass",
    "heavy bass": "bass",
    "bass heavy": "bass",
    "bassy": "bass",
    "808": "bass",
    "808s": "bass",
    "sub bass": "bass",
    "synthesizer": "synth",
    "synthy": "synth",
    "keyboard": "keys",
    "keys": "keys",
    "organ": "keys",
    "rhodes": "keys",
    "electric piano": "keys",
    "riff": "riffs",
    "riffs": "riffs",
    "guitar riff": "riffs",
    "guitar riffs": "riffs",
    "shredding": "riffs",
    "shred": "riffs",
    "slap bass": "bass",
    "drum": "drums",
    "drumming": "drums",
    "drum solo": "drums",
    "breakbeat drums": "drums",
    "electric guitar": "guitar",
    "guitars": "guitar",
    "fingerpicking": "acoustic",
    "violin": "strings",
    "cello": "strings",
    "orchestra": "orchestral",
    "trumpet": "horns",
    "brass section": "horns",
    "flute": "woodwinds",
    "clarinet": "woodwinds",
    "harmonica": "harmonica",
    "banjo": "twangy",
    "ukulele": "ukulele",
    "handclaps": "handclaps",
    "claps": "handclaps",
    "whistling": "whistling",
    "samples": "sampled",
    "sample": "sampled",
    "sampling": "sampled",
    "chopped": "sampled",
    # --- voice
    "female vocal": "female vocals",
    "female voice": "female vocals",
    "male vocal": "male vocals",
    "male voice": "male vocals",
    "falsetto": "falsetto",
    "harmonies": "harmonies",
    "harmony": "harmonies",
    "vocal harmonies": "harmonies",
    "choir": "choir",
    "choral": "choir",
    "spoken word": "spoken word",
    "storytelling": "storytelling",
    "lyrical": "lyrical",
    "wordplay": "lyrical",
    "great lyrics": "lyrical",
    "raspy": "raspy",
    "gravelly": "raspy",
    "whisper": "soft vocals",
    "breathy": "soft vocals",
    "crooner": "smooth",
    "crooning": "smooth",
    "belting": "powerful vocals",
    "powerful vocals": "powerful vocals",
    "screaming": "screaming",
    "screamo vocals": "screaming",
    "growl": "screaming",
    "chant": "chanting",
    "chanting": "chanting",
    "autotune": "auto-tune",
    "auto tune": "auto-tune",
    "no vocals": "instrumental",
    "without vocals": "instrumental",
    # --- settings & moments
    "summer vibes": "summer",
    "summertime": "summer",
    "pool party": "party",
    "club": "party",
    "clubbing": "party",
    "pregame": "party",
    "winter": "winter",
    "snow": "winter",
    "autumn": "autumn",
    "spring": "spring",
    "christmas": "holiday",
    "holiday": "holiday",
    "nighttime": "late night",
    "3am": "late night",
    "2am": "late night",
    "golden hour": "sunset",
    "dusk": "sunset",
    "sunrise": "morning",
    "wake up": "morning",
    "coffee": "coffee shop",
    "coffee shop": "coffee shop",
    "cafe": "coffee shop",
    "campfire": "campfire",
    "road": "driving",
    "car": "driving",
    "windows down": "driving",
    "drive": "driving",
    "head bob": "groovy",
    "head bobbing": "groovy",
    "bob my head": "groovy",
    "head nod": "groovy",
    "head nodding": "groovy",
    "lost in the groove": "groovy",
    "in the pocket": "groovy",
    "hooky": "catchy",
    "hooks you": "catchy",
    "cruising": "driving",
    "highway": "driving",
    "gaming": "gaming",
    "video games": "gaming",
    "cooking": "cooking",
    "cleaning": "upbeat",
    "dinner": "dinner",
    "date night": "romantic",
    "wedding": "romantic",
    "bedroom": "intimate",
    "rainy": "rainy day",
    "storm": "rainy day",
    "ocean": "summer",
    "tropical": "tropical",
    "island": "tropical",
    "city": "urban",
    "urban": "urban",
    "nature": "nature",
    "forest": "nature",
    "space travel": "atmospheric",
    "soundscape": "atmospheric",
    "background": "background",
    "background music": "background",
    "earworm": "catchy",
    "hook": "catchy",
    "hooks": "catchy",
    "singable": "sing-along",
    "karaoke": "sing-along",
    "throwback": "nostalgic",
    "timeless": "timeless",
    "underrated": "underrated",
    "hidden gem": "underrated",
}
# every canonical vibe word is also a vibe word itself ("chill", "sad", "relaxed", ...)
for _v in set(VIBE_ALIASES.values()):
    VIBE_ALIASES.setdefault(_v, _v)

# Genres that also describe the sound: they keep their genre label (for playlists that ask
# for the genre) and add these vibe words at SOUND_GENRE_WEIGHT.
SOUND_OF: dict[str, dict[str, float]] = {
    "jazz": {"jazzy": 1.0},
    "jazz hop": {"jazzy": 1.0, "lo-fi": 0.8, "chill": 0.8},
    "jazzhop": {"jazzy": 1.0, "lo-fi": 0.8, "chill": 0.8},
    "chillhop": {"chill": 1.0, "lo-fi": 0.8, "jazzy": 0.6},
    "lo-fi hip hop": {"lo-fi": 1.0, "chill": 0.8},
    "acid jazz": {"jazzy": 1.0, "groovy": 0.8},
    "smooth jazz": {"jazzy": 1.0, "smooth": 1.0},
    "nu jazz": {"jazzy": 1.0},
    "funk": {"funky": 1.0, "groovy": 0.8},
    "p-funk": {"funky": 1.0},
    "funk rock": {"funky": 1.0},
    "soul": {"soulful": 1.0},
    "neo soul": {"soulful": 1.0, "smooth": 0.6},
    "neo-soul": {"soulful": 1.0, "smooth": 0.6},
    "disco": {"groovy": 1.0, "danceable": 1.0},
    "nu disco": {"groovy": 1.0, "danceable": 1.0},
    "house": {"danceable": 1.0},
    "deep house": {"danceable": 0.8, "chill": 0.5},
    "ambient": {"atmospheric": 1.0, "calm": 0.6},
    "downtempo": {"chill": 1.0, "slow": 0.6},
    "trip hop": {"moody": 1.0, "chill": 0.6},
    "trip-hop": {"moody": 1.0, "chill": 0.6},
    "chillwave": {"chill": 1.0, "dreamy": 0.8},
    "dream pop": {"dreamy": 1.0},
    "shoegaze": {"dreamy": 1.0, "loud": 0.5},
    "psychedelic rock": {"psychedelic": 1.0},
    "neo-psychedelia": {"psychedelic": 1.0},
    "synthwave": {"synth": 1.0, "nostalgic": 0.6},
    "synthpop": {"synth": 1.0},
    "electropop": {"synth": 0.8},
    "new wave": {"synth": 0.6},
    "electronic": {"synth": 0.5},
    "electronica": {"synth": 0.5},
    "metal": {"heavy": 1.0},
    "heavy metal": {"heavy": 1.0},
    "nu metal": {"heavy": 1.0, "aggressive": 0.6},
    "metalcore": {"heavy": 1.0, "aggressive": 0.8},
    "hard rock": {"heavy": 0.8, "loud": 0.6},
    "grunge": {"heavy": 0.7, "raw": 0.6},
    "punk": {"raw": 0.8, "fast": 0.6},
    "punk rock": {"raw": 0.8, "fast": 0.6},
    "post-hardcore": {"heavy": 0.8, "intense": 0.6},
    "post-rock": {"atmospheric": 1.0, "epic": 0.6},
    "indie": {"indie": 1.0},
    "indie rock": {"indie": 1.0},
    "indie pop": {"indie": 1.0, "catchy": 0.5},
    "indie folk": {"indie": 1.0, "acoustic": 0.7},
    "bedroom pop": {"indie": 1.0, "lo-fi": 0.6},
    "folk": {"acoustic": 0.8},
    "singer-songwriter": {"acoustic": 0.6},
    "singer songwriter": {"acoustic": 0.6},
    "bossa nova": {"mellow": 1.0, "jazzy": 0.6},
    "reggae": {"laid-back": 1.0},
    "surf rock": {"summer": 0.8},
    "surf": {"summer": 0.8},
    "classical": {"orchestral": 0.8},
    "soundtrack": {"cinematic": 0.8},
    "score": {"cinematic": 0.8},
    "video game music": {"cinematic": 0.5},
    "emo": {"emotional": 0.8},
    "slowcore": {"slow": 1.0, "melancholic": 0.6},
    "sadcore": {"sad": 1.0},
    "blues": {"soulful": 0.6},
    "gospel": {"soulful": 0.8},
    "r&b": {"smooth": 0.6},
    "drum and bass": {"fast": 0.8, "danceable": 0.6},
    "dubstep": {"bass": 1.0, "heavy": 0.5},
    "edm": {"danceable": 0.8, "energetic": 0.6},
    "trance": {"danceable": 0.8, "energetic": 0.6},
    "techno": {"danceable": 0.8},
    "jazz rap": {"jazzy": 1.0, "sampled": 0.6},
    "boom bap": {"drums": 0.8, "sampled": 0.8},
    "abstract hip hop": {"experimental": 0.8, "sampled": 0.6},
    "instrumental hip hop": {"instrumental": 1.0, "beats": 1.0, "sampled": 0.6},
    "cloud rap": {"dreamy": 0.8, "atmospheric": 0.6},
    "trap": {"bass": 0.8, "drums": 0.5},
    "drill": {"dark": 0.8, "bass": 0.6},
    "phonk": {"dark": 0.8, "bass": 0.8},
    "emo rap": {"emotional": 1.0, "sad": 0.6},
    "math rock": {"riffs": 0.8, "rhythmic": 0.8},
    "post-punk": {"dark": 0.6, "bass": 0.6},
    "garage rock": {"raw": 1.0, "gritty": 0.6},
    "stoner rock": {"heavy": 0.8, "fuzzy": 0.8},
    "noise rock": {"noisy": 1.0, "distorted": 0.8},
    "blues rock": {"riffs": 0.8, "soulful": 0.5},
    "classic rock": {"riffs": 0.5},
    "funk metal": {"funky": 1.0, "heavy": 0.8},
    "alternative metal": {"heavy": 0.8},
    "progressive rock": {"epic": 0.6, "experimental": 0.5},
    "prog rock": {"epic": 0.6, "experimental": 0.5},
    "art rock": {"experimental": 0.6},
    "yacht rock": {"smooth": 1.0, "breezy": 0.8},
    "soft rock": {"soft": 0.6, "smooth": 0.6},
    "power pop": {"catchy": 1.0, "upbeat": 0.6},
    "pop punk": {"upbeat": 0.8, "fast": 0.6, "angsty": 0.6},
    "britpop": {"catchy": 0.6, "jangly": 0.5},
    "jangle pop": {"jangly": 1.0},
    "chamber pop": {"strings": 0.8, "lush": 0.6},
    "baroque pop": {"strings": 0.8, "lush": 0.6},
    "art pop": {"experimental": 0.6},
    "hyperpop": {"glitchy": 1.0, "frantic": 0.6},
    "city pop": {"breezy": 0.8, "smooth": 0.6},
    "vaporwave": {"dreamy": 0.8, "lo-fi": 0.8, "nostalgic": 0.6},
    "lo-fi house": {"lo-fi": 1.0, "danceable": 0.6},
    "future bass": {"synth": 0.8, "drops": 0.8},
    "big room": {"drops": 1.0, "euphoric": 0.6},
    "progressive house": {"builds up": 0.8, "euphoric": 0.6},
    "electro house": {"drops": 0.8, "energetic": 0.6},
    "garage": {"rhythmic": 0.6},
    "uk garage": {"rhythmic": 0.8, "bouncy": 0.6},
    "breakbeat": {"drums": 0.8, "rhythmic": 0.6},
    "idm": {"glitchy": 0.8, "experimental": 0.6},
    "industrial": {"distorted": 0.8, "dark": 0.6},
    "darkwave": {"dark": 1.0, "synth": 0.8},
    "coldwave": {"dark": 0.8, "synth": 0.8},
    "witch house": {"dark": 1.0, "hypnotic": 0.6},
    "krautrock": {"hypnotic": 1.0, "rhythmic": 0.6},
    "space rock": {"atmospheric": 1.0, "psychedelic": 0.6},
    "afrobeat": {"rhythmic": 1.0, "groovy": 0.8, "horns": 0.6},
    "afrobeats": {"danceable": 0.8, "bouncy": 0.6},
    "latin": {"rhythmic": 0.6},
    "salsa": {"rhythmic": 0.8, "horns": 0.6},
    "samba": {"rhythmic": 0.8},
    "dub": {"bass": 1.0, "reverb": 0.8},
    "ska": {"upbeat": 0.8, "horns": 0.8},
    "swing": {"swinging": 1.0, "horns": 0.6},
    "big band": {"horns": 1.0, "swinging": 0.8},
    "bebop": {"jazzy": 1.0, "fast": 0.6},
    "cool jazz": {"jazzy": 1.0, "mellow": 0.8},
    "jazz fusion": {"jazzy": 1.0, "groovy": 0.6},
    "fusion": {"jazzy": 0.6},
    "doo wop": {"harmonies": 1.0, "nostalgic": 0.6},
    "motown": {"soulful": 1.0, "groovy": 0.6},
    "northern soul": {"soulful": 1.0, "upbeat": 0.6},
    "philly soul": {"soulful": 1.0, "strings": 0.6},
    "quiet storm": {"smooth": 1.0, "romantic": 0.8},
    "slow jams": {"slow": 0.8, "romantic": 0.8},
    "contemporary r&b": {"smooth": 0.8},
    "alternative r&b": {"moody": 0.8, "atmospheric": 0.6},
    "bluegrass": {"twangy": 1.0, "acoustic": 0.8},
    "americana": {"acoustic": 0.6, "twangy": 0.5},
    "country": {"twangy": 0.6},
    "alt-country": {"twangy": 0.6},
    "outlaw country": {"twangy": 0.8, "raw": 0.6},
    "folk rock": {"acoustic": 0.6, "jangly": 0.5},
    "freak folk": {"quirky": 0.8, "acoustic": 0.6},
    "anti-folk": {"quirky": 0.8, "lo-fi": 0.5},
    "chamber folk": {"strings": 0.8, "acoustic": 0.6},
    "new age": {"meditative": 1.0, "calm": 0.8},
    "drone": {"hypnotic": 1.0, "atmospheric": 0.8},
    "minimalism": {"minimal": 1.0, "hypnotic": 0.6},
    "modern classical": {"piano": 0.6, "strings": 0.6, "cinematic": 0.5},
    "neoclassical": {"piano": 0.6, "strings": 0.6},
    "opera": {"dramatic": 1.0, "powerful vocals": 0.8},
    "baroque": {"strings": 0.8, "orchestral": 0.6},
    "musical": {"dramatic": 0.8, "theatrical": 0.6},
    "a cappella": {"harmonies": 1.0},
    "gregorian": {"choir": 1.0, "meditative": 0.6},
    "christmas music": {"holiday": 1.0},
    "anime": {"upbeat": 0.4},
    "video game": {"gaming": 1.0},
    "chiptune": {"gaming": 1.0, "synth": 0.8},
    "8-bit": {"gaming": 1.0, "synth": 0.8},
}

GENRE_EXACT = {
    "rock",
    "pop",
    "rap",
    "hip hop",
    "alternative",
    "alternative rock",
    "classic rock",
    "pop rock",
    "soft rock",
    "folk rock",
    "garage rock",
    "progressive rock",
    "prog rock",
    "art rock",
    "blues rock",
    "southern rock",
    "britpop",
    "post-punk",
    "pop punk",
    "hardcore",
    "screamo",
    "country",
    "americana",
    "bluegrass",
    "latin",
    "reggaeton",
    "k-pop",
    "j-pop",
    "city pop",
    "underground hip hop",
    "east coast hip hop",
    "west coast hip hop",
    "gangsta rap",
    "conscious hip hop",
    "alternative hip hop",
    "experimental hip hop",
    "abstract hip hop",
    "instrumental hip hop",
    "jazz rap",
    "boom bap",
    "dirty south",
    "grime",
    "drill",
    "uk hip hop",
    "cloud rap",
    "emo rap",
    "dance",
    "dance pop",
    "electro",
    "idm",
    "big beat",
    "breakbeat",
    "industrial",
    "musical",
    "christian",
    "world",
    "ska",
    "power pop",
    "art pop",
    "chamber pop",
    "baroque pop",
    "noise rock",
    "math rock",
    "midwest emo",
    "alt-country",
    "rockabilly",
    "doo wop",
    "swing",
    "big band",
    "bebop",
    "hard bop",
    "cool jazz",
    "fusion",
    "jazz fusion",
    "rnb",
    "contemporary r&b",
    "soul jazz",
    "anime",
    "opera",
    "baroque",
    "romantic era",
    "minimalism",
    "pop soul",
    "philly soul",
    "northern soul",
    "electro swing",
}
GENRE_PATTERN = re.compile(
    r"\b(rock|hip hop|rap|metal|punk|pop|core|country|trap|drill|r&b|techno|trance|house|"
    r"step|edm|wave|jazz|soul|funk|folk|blues|disco|hop|gaze|grunge|emo)\b"
)
ERA_PATTERN = re.compile(r"^\d0s$")
ERA_WORDS = {"oldies", "golden oldies", "retro", "classic", "classics", "throwback", "vintage"}
IGNORE_EXACT = {
    "american",
    "british",
    "english",
    "uk",
    "usa",
    "us",
    "canadian",
    "australian",
    "irish",
    "scottish",
    "french",
    "german",
    "japanese",
    "korean",
    "swedish",
    "icelandic",
    "brazilian",
    "mexican",
    "spanish",
    "italian",
    "dutch",
    "norwegian",
    "danish",
    "finnish",
    "new zealand",
    "los angeles",
    "new york",
    "london",
    "california",
    "seattle",
    "chicago",
    "detroit",
    "atlanta",
    "texas",
    "cover",
    "covers",
    "live",
    "remix",
    "remixes",
    "single",
    "album",
    "albums",
    "ep",
    "hits",
    "top 40",
    "radio",
    "chart",
    "billboard",
    "grammy",
    "male",
    "female",
    "band",
    "duo",
    "producer",
    "rapper",
    "singer",
}
IGNORE_PATTERN = re.compile(
    r"^\d{4}$|^\d{4}s?$|\b(records|recordings|label)\b"
    r"|\b\d{2,3}(\.\d)?\s*fm\b|\bfm\b|\bradio\b|\bstation\b|\bdj\b|\bplaylist\b"
    r"|\bwith\b|\bpodcast\b|\bmixtape\b|\bshow\b|\bepisode\b|\bvol\b"
)


def default_class(tag: str) -> str:
    """Built-in class for a normalized tag (before your overrides)."""
    if tag in VIBE_ALIASES:
        return "vibe"
    if ERA_PATTERN.match(tag) or tag in ERA_WORDS:
        return "era"
    if tag in IGNORE_EXACT or IGNORE_PATTERN.search(tag):
        return "ignore"
    if tag in SOUND_OF or tag in GENRE_EXACT or GENRE_PATTERN.search(tag):
        return "genre"
    return "other"


@dataclass
class TagClasses:
    overrides: dict[str, str] = field(default_factory=dict)

    def of(self, tag: str) -> str:
        return self.overrides.get(tag) or default_class(tag)

    def is_label(self, tag: str) -> bool:
        return self.of(tag) in ("genre", "era")


def load_classes(db: Database | None) -> TagClasses:
    if db is None:
        return TagClasses()
    try:
        rows = db.conn.execute("SELECT tag, class FROM tag_classes").fetchall()
    except Exception:  # older schema in a test helper
        return TagClasses()
    return TagClasses({r[0]: r[1] for r in rows})


def set_class(db: Database, tag: str, cls: str | None) -> None:
    """Reclassify a tag (None = back to the built-in rule)."""
    tag = tag.strip().lower()
    if cls is None:
        db.conn.execute("DELETE FROM tag_classes WHERE tag=?", (tag,))
        return
    if cls not in CLASSES:
        raise ValueError(f"class must be one of {', '.join(CLASSES)}")
    db.conn.execute(
        "INSERT OR REPLACE INTO tag_classes (tag, class, updated_at) VALUES (?,?,?)",
        (tag, cls, utcnow()),
    )


SONG_SOURCES = ("lastfm_track", "mb_recording", "acousticbrainz")
ARTIST_SOURCES = ("lastfm_artist", "mb_artist")


@dataclass
class SongProfile:
    vibe: dict[str, float]  # always used
    labels: dict[str, float]  # genre / era: used only by playlists that ask for them
    evidence: str  # song | artist (fallback) | none

    @property
    def merged(self) -> dict[str, float]:
        out = dict(self.labels)
        for t, w in self.vibe.items():
            out[t] = max(out.get(t, 0.0), w)
        return out


def _fold(
    tags: dict[str, float],
    classes: TagClasses,
    vibe: dict[str, float],
    labels: dict[str, float],
    scale: float = 1.0,
) -> None:
    def put(d: dict[str, float], k: str, w: float) -> None:
        if w > 0:
            d[k] = max(d.get(k, 0.0), round(w * scale, 4))

    for tag, w in tags.items():
        cls = classes.of(tag)
        if cls == "ignore":
            continue
        if cls == "vibe":
            put(vibe, VIBE_ALIASES.get(tag, tag), w)
        elif cls == "other":
            put(vibe, tag, OTHER_WEIGHT * w)
        else:  # genre / era
            put(labels, tag, w)
            for adj, f in SOUND_OF.get(tag, {}).items():
                put(vibe, adj, SOUND_GENRE_WEIGHT * f * w)


# Related words, so a description and the data meet even when they use different words: the
# audio data says "relaxed" and "fast", people say "chill" and "energetic". A song carrying the
# word on the left also counts for the words on the right, at the given share of its weight
# (one step only, never chained). Fixed, readable rules.
IMPLIES: dict[str, dict[str, float]] = {
    "relaxed": {"chill": 0.8, "calm": 0.7, "mellow": 0.7, "laid-back": 0.6},
    "chill": {"relaxed": 0.8, "mellow": 0.7, "laid-back": 0.7, "calm": 0.5},
    "mellow": {"chill": 0.7, "relaxed": 0.7, "soft": 0.5, "calm": 0.5},
    "calm": {"relaxed": 0.8, "chill": 0.6, "soft": 0.6, "sleep": 0.5},
    "laid-back": {"chill": 0.7, "relaxed": 0.6},
    "soft": {"calm": 0.5, "mellow": 0.5},
    "slow": {"calm": 0.4, "sleep": 0.4},
    "minimal": {"calm": 0.3},
    "happy": {"feel-good": 0.8, "upbeat": 0.5, "cheerful": 0.7},
    "cheerful": {"happy": 0.8, "feel-good": 0.7, "upbeat": 0.5},
    "feel-good": {"happy": 0.8, "upbeat": 0.6},
    "upbeat": {"energetic": 0.5, "happy": 0.4},
    "party": {"energetic": 0.6, "hype": 0.6, "danceable": 0.6, "upbeat": 0.6},
    "danceable": {"groovy": 0.5, "upbeat": 0.5, "party": 0.4},
    "groovy": {"danceable": 0.5, "funky": 0.4},
    "funky": {"groovy": 0.8, "danceable": 0.4},
    "aggressive": {"intense": 0.8, "energetic": 0.7, "heavy": 0.5, "hype": 0.5},
    "intense": {"energetic": 0.7, "aggressive": 0.5, "hype": 0.5},
    "energetic": {"upbeat": 0.6, "hype": 0.6, "intense": 0.4, "workout": 0.4},
    "hype": {"energetic": 0.8, "workout": 0.5},
    "heavy": {"intense": 0.6, "loud": 0.6, "energetic": 0.4},
    "fast": {"energetic": 0.5, "upbeat": 0.4},
    "busy": {"energetic": 0.4},
    "passionate": {"emotional": 0.6, "powerful": 0.5, "energetic": 0.3},
    "sad": {"melancholic": 0.8, "emotional": 0.5},
    "melancholic": {"sad": 0.7, "bittersweet": 0.5, "moody": 0.5},
    "bittersweet": {"melancholic": 0.6, "nostalgic": 0.4, "emotional": 0.5},
    "dark": {"moody": 0.7},
    "moody": {"dark": 0.5, "late night": 0.4},
    "dreamy": {"atmospheric": 0.6, "chill": 0.4},
    "atmospheric": {"dreamy": 0.5, "calm": 0.3},
    "synth": {"electronic": 0.6},
    "electronic": {"synth": 0.6},
    "acoustic": {"soft": 0.3, "guitar": 0.4},
    "lo-fi": {"chill": 0.6, "focus": 0.5},
    "instrumental": {"focus": 0.5},
    "jazzy": {"smooth": 0.4},
    "soulful": {"smooth": 0.4, "emotional": 0.4},
    "smooth": {"mellow": 0.4},
    "riffs": {"guitar": 0.8},
    "playful": {"quirky": 0.6, "happy": 0.4},
    "summer": {"feel-good": 0.5, "upbeat": 0.4},
    "driving": {"upbeat": 0.4},
}


def _expand(vibe: dict[str, float]) -> None:
    """Add related words (one step) at a share of the original weight; keeps the max."""
    implied: dict[str, float] = {}
    for word, w in vibe.items():
        for other, f in IMPLIES.get(word, {}).items():
            implied[other] = max(implied.get(other, 0.0), round(w * f, 4))
    for other, w in implied.items():
        if w > vibe.get(other, 0.0):
            vibe[other] = w


def profile(by_source: dict[str, dict[str, float]], classes: TagClasses) -> SongProfile:
    """How one song sounds, from its own data first; artist tags only as a flagged fallback."""
    vibe: dict[str, float] = {}
    labels: dict[str, float] = {}
    for src in SONG_SOURCES:
        if by_source.get(src):
            _fold(by_source[src], classes, vibe, labels)
    if vibe or labels:
        _expand(vibe)
        return SongProfile(vibe, labels, "song")
    for src in ARTIST_SOURCES:
        if by_source.get(src):
            _fold(by_source[src], classes, vibe, labels, scale=ARTIST_FALLBACK)
    _expand(vibe)
    return SongProfile(vibe, labels, "artist" if (vibe or labels) else "none")


def requested_labels(rules: dict[str, Any], classes: TagClasses) -> set[str]:
    """Genre / era tags a playlist definition explicitly mentions (so they count there)."""
    mentioned = set((rules.get("tags") or {}).keys())
    mentioned |= set(rules.get("require_tags") or []) | set(rules.get("avoid_tags") or [])
    return {t for t in mentioned if classes.is_label(t)}


_GENERIC = {"music", "songs", "song", "tracks", "stuff", "sounds", "vibes", "the", "and"}


def _plain(text: str) -> str:
    return f" {re.sub(r'[^a-z0-9&]+', ' ', text.lower())} "


def _typed(tag: str, text: str) -> bool:
    """Did you type this genre / era (the whole phrase, or every meaningful word of it)?"""
    low = _plain(text)
    if f" {tag.replace('-', ' ')} " in low:
        return True
    parts = [p for p in re.split(r"[\s-]+", tag) if len(p) > 2 and p not in _GENERIC]
    return bool(parts) and all(f" {p} " in low for p in parts)


_DECADE_TEXT = re.compile(r"\b(?:19)?([5-9]0)'?s\b|\b20([0-2]0)'?s\b")


def labels_in(text: str, weight: float = 0.9) -> dict[str, float]:
    """Genre and era words you typed ("rap", "alternative rock", "90s and 2000s") -> tags.
    Typing a genre or era is how a playlist asks for it."""
    low = _plain(text)
    found: dict[str, float] = {}
    for m in _DECADE_TEXT.finditer(text.lower()):
        found[f"{m.group(1) or m.group(2)}s"] = weight
    known = GENRE_EXACT | set(SOUND_OF) | ERA_WORDS
    for phrase in sorted(known, key=len, reverse=True):
        key = f" {phrase.replace('-', ' ')} "
        if key in low and default_class(phrase) in ("genre", "era"):
            found.setdefault(phrase, weight)
            low = low.replace(key, " | ")
    return found


def words_in(text: str, weight: float = 0.9) -> dict[str, float]:
    """Vibe words you typed ("bouncy, falsetto, windows down") -> {canonical word: weight}.
    Longest phrases first, so "late night" isn't also read as "night"."""
    low = f" {re.sub(r'[^a-z0-9&]+', ' ', text.lower())} "
    found: dict[str, float] = {}
    for phrase in sorted(VIBE_ALIASES, key=len, reverse=True):
        key = f" {phrase.replace('-', ' ')} "
        if key in low:
            found.setdefault(VIBE_ALIASES[phrase], weight)
            low = low.replace(key, " | ")
    return found


def vibe_only_avoid(avoid: list[str], text: str, classes: TagClasses) -> list[str]:
    """Genre / era words to avoid survive only if you typed them; a sound genre you didn't
    type becomes its strongest vibe words ("no metal" from a 'study' preset -> avoid heavy)."""
    out: list[str] = []
    for tag in avoid:
        if not classes.is_label(tag) or _typed(tag, text):
            out.append(VIBE_ALIASES.get(tag, tag) if classes.of(tag) == "vibe" else tag)
            continue
        out += [adj for adj, f in SOUND_OF.get(tag, {}).items() if f >= 0.8]
    return list(dict.fromkeys(out))


def vibe_only(tags: dict[str, float], text: str, classes: TagClasses) -> dict[str, float]:
    """Filter a description recipe: genre / era tags survive only if you typed them (or a word
    of them); otherwise a sound genre turns into its vibe words and a pure genre is dropped."""
    out: dict[str, float] = {}
    for tag, w in tags.items():
        if not classes.is_label(tag):
            key = VIBE_ALIASES.get(tag, tag) if classes.of(tag) == "vibe" else tag
            out[key] = max(out.get(key, 0.0), w)
            continue
        if _typed(tag, text):
            out[tag] = max(out.get(tag, 0.0), w)
        for adj, f in SOUND_OF.get(tag, {}).items():
            out[adj] = max(out.get(adj, 0.0), round(SOUND_GENRE_WEIGHT * f * w, 2))
    return out
