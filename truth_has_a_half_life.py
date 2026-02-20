"""
Truth Has a Half Life — Cinematic Vertical Slice
==============================================

A narrative-driven, time-pressure memory investigation game. Assume the role
of a prosecutor with a limited window into a dying victim's mind. Select
clocks to enter memory scenes; evidence fades as the victim's memory degrades.
Press S to crystallise a snapshot (max 3). Accuse a suspect based on preserved
evidence tags.

Run: python truth_has_a_half_life.py

BACKGROUND MUSIC (credit required by license)
--------------------------------------------
Background track: Yakov Golman (Piano & orchestra, Classical, Instrumental).
Source: Free Music Archive. License: CC BY.

ADDING YOUR OWN SPRITES / DRAWINGS LATER
----------------------------------------
- Menu clocks: In _draw_menu_impl(), replace the draw_glowing_circle() + tick
  line with a blit of your clock image centered at (cx, cy). Use _clock_center(idx).
- Memory scene background: In _build_scenes(), after creating the gradient bg,
  load and blit your room image onto bg (e.g. pygame.image.load("room.png"))
  or use a different image per scene label.
- Evidence: Each scene has SceneArtifact(s) with image, frac position, and tags.
  Add more artifacts in _build_scenes and adjust frac_x/frac_y for placement.
- Accusation cards: In draw_accuse(), the suspect cards are drawn with
  pygame.draw.rect and text. Add a card background image and blit it per card
  before drawing name/role/motive.
- Result screen: In draw_result(), same idea: optional background image or
  sprite for CASE CLOSED / MEMORY COLLAPSED.
"""

import io
import math
import os
import random
import struct
import wave
from array import array
from dataclasses import dataclass
from typing import List, Tuple

try:
    import pygame
except ImportError as exc:
    raise SystemExit(
        "This game requires Pygame. Install with: pip install pygame"
    ) from exc


# ---------------------------------------------------------------------------
# Procedural audio (no external files)
# ---------------------------------------------------------------------------

def _make_wav_bytes(sample_rate: int, duration_sec: float, generator) -> bytes:
    """Generate WAV file bytes from a sample generator (yields -1..1 floats)."""
    n_samples = int(sample_rate * duration_sec)
    samples = array("h")
    for i in range(n_samples):
        t = i / sample_rate
        v = generator(t, i, n_samples)
        v = max(-1.0, min(1.0, v))
        samples.append(int(v * 32767))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(samples.tobytes())
    buf.seek(0)
    return buf.read()


def _procedural_sounds() -> dict:
    """Create heartbeat, snapshot beep, and ominous tone as pygame Sounds."""
    sr = 22050
    sounds = {}

    def _load_wav(wav_bytes: bytes):
        return pygame.mixer.Sound(file=io.BytesIO(wav_bytes))

    # Heartbeat: two thumps
    def heartbeat_gen(t, i, n):
        period = 0.8
        phase = (t % period) / period
        if phase < 0.15:
            return math.exp(-phase * 40) * 0.4 * math.sin(phase * 80)
        if phase < 0.35:
            return math.exp(-(phase - 0.2) * 30) * 0.35 * math.sin((phase - 0.2) * 70)
        return 0.0

    sounds["heartbeat"] = _load_wav(_make_wav_bytes(sr, 0.9, heartbeat_gen))
    sounds["heartbeat"].set_volume(0.25)

    # Snapshot: short low beep
    def beep_gen(t, i, n):
        if t > 0.12:
            return 0.0
        return 0.3 * math.sin(2 * math.pi * 440 * t) * math.exp(-t * 15)

    sounds["snapshot"] = _load_wav(_make_wav_bytes(sr, 0.2, beep_gen))
    sounds["snapshot"].set_volume(0.4)

    # Ominous low tone (loopable)
    def ominous_gen(t, i, n):
        return 0.12 * math.sin(2 * math.pi * 55 * t) * (0.7 + 0.3 * math.sin(0.5 * t))

    sounds["ominous"] = _load_wav(_make_wav_bytes(sr, 2.0, ominous_gen))
    sounds["ominous"].set_volume(0.2)

    return sounds


# ---------------------------------------------------------------------------
# Easing and animation helpers
# ---------------------------------------------------------------------------

def ease_out_quad(t: float) -> float:
    return 1.0 - (1.0 - t) ** 2


def ease_in_out_cubic(t: float) -> float:
    if t < 0.5:
        return 4 * t * t * t
    return 1 - (-2 * t + 2) ** 3 / 2


def ease_out_elastic(t: float) -> float:
    if t <= 0 or t >= 1:
        return t
    c4 = 2 * math.pi / 3
    return 2 ** (-10 * t) * math.sin((t * 10 - 0.75) * c4) + 1


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SceneArtifact:
    """One artifact image placed in a scene (position as fraction of background image)."""
    surface: pygame.Surface
    frac_x: float  # 0–1, position on background image
    frac_y: float
    tags: List[str]
    rotation_degrees: float = 0.0  # counterclockwise
    darken: float = 1.0  # 1 = normal, <1 = darker (same fade as bg when drawn before overlay)
    offset_x_aw: float = 0.0  # draw-time shift in artifact-widths (e.g. 1 = one width east)
    offset_y_ah: float = 0.0  # draw-time shift in artifact-heights (e.g. 1 = one height south)
    spec_filename: str = ""  # e.g. "q10-1.png" or "r3.png" for popup name/description lookup
    suspect_id: str = ""  # "queen" | "chef" | "goblin" for evidence scoring
    points: int = 0  # evidence points toward that suspect (0 for replacements)


@dataclass
class MemoryScene:
    label: str
    background: pygame.Surface
    bg_rect: Tuple[int, int, int, int]  # (ox, oy, w, h) of image area in scene
    artifacts: List[SceneArtifact]


# Artifact popup: display name and description keyed by spec filename (e.g. "q10-1.png")
ARTIFACT_INFO: dict = {
    "q10-1.png": {
        "name": "Hidden Poison Vial",
        "description": "A glass vial hidden with deliberate care. The alchemical markings are faint but intentional. This was not misplaced — it was concealed. The chest in which it was found belongs to the queen.",
    },
    "q10-2.png": {
        "name": "Altered Succession Decree",
        "description": "The parchment bears signs of revision. Names scratched away. Lines rewritten. Authority was exercised here — quietly, and without witnesses.",
    },
    "q5-1.png": {
        "name": "Dismissed Guard Order",
        "description": "An order bearing the royal crest, reducing protection at a crucial hour. The ink has not yet fully dried. The note reads: Reduce guard presence outside my chambers.",
    },
    "q5-2.png": {
        "name": "Uneaten Royal Dish",
        "description": "Prepared, presented… and left untouched. The food has gone cold, as if the moment to eat never came.",
    },
    "q1-1.png": {
        "name": "Personal Letter of Fear",
        "description": "The words speak of unease. Of doubt. Of someone close who could not be trusted — though no name is written. The letter reads: I fear I have made a terrible mistake, This weights heaavy upon me. Each night I am tormented by doubt and dears. What if I have laid us on the wrong path? I am so frightful of what may come.",
    },
    "q1-2.png": {
        "name": "Fractured Farewell Memory Anchor",
        "description": "A handkerchief, folded carefully despite its stains. Whatever was felt here was never spoken aloud.",
    },
    "c10-1.png": {
        "name": "Toxin Residue on Prep Board",
        "description": "The residue does not belong to any spice or herb used in royal kitchens. It was scrubbed — but not well enough.",
    },
    "c10-2.png": {
        "name": "Over-Cleaned Warped Knife",
        "description": "The blade is spotless. Too spotless. Heat has bent the metal ever so slightly, as if something needed to be erased.",
    },
    "c5-1.png": {
        "name": "Altered Recipe Ledger",
        "description": "Ingredients crossed out. Measurements rewritten. A recipe adjusted in haste — or intent.",
    },
    "c5-2.png": {
        "name": "Replaced Drinking Cup",
        "description": "One cup sits where another once stood. Small changes can matter more than grand gestures.",
    },
    "c1-1.png": {
        "name": "Prideful Personal Note",
        "description": "The note speaks of honor in service, of feeding kings and shaping history from the shadows of the kitchen. The note reads: A pride to serve not one but three kings",
    },
    "c1-2.png": {
        "name": "Discarded Kitchen Token",
        "description": "A simple token, worn smooth by years of use. It carries no message — only familiarity.",
    },
    "g10-1.png": {
        "name": "Ash-Slime Residue",
        "description": "Ash mixed with something unnatural. The substance clings stubbornly to stone, as if it does not wish to be forgotten.",
    },
    "g10-2.png": {
        "name": "Bone-Shard Tool",
        "description": "Crude, sharpened, and carefully hidden. Made to pierce — not to repair.",
    },
    "g5-1.png": {
        "name": "Furnace Rekindling Marks",
        "description": "Fresh scorch marks where none should be. Someone returned to the fire after nightfall.",
    },
    "g5-2.png": {
        "name": "Goblin Work Sigil",
        "description": "A maintenance mark, warm to the touch. It signifies duty — or presence — in places few are meant to notice.",
    },
    "g1-1.png": {
        "name": "Cracked Goblin Totem",
        "description": "A Goblin's claw — the charm is broken, split by force or neglect. Superstition says broken totems bring ill fortune.",
    },
    "g1-2.png": {
        "name": "Memory Silhouette Anchor",
        "description": "A shape lingers where something once stood. The memory refuses to fully vanish.",
    },
    # Replacement artifacts (0 points; used when killer is not that suspect)
    "r1.png": {"name": "Cracked Royal Signet Seal", "description": "The crest is damaged, its authority weakened. Ownership cannot be determined."},
    "r2.png": {"name": "Torn Court Petition Fragment", "description": "A grievance, perhaps. Or merely frustration torn from parchment and forgotten."},
    "r3.png": {"name": "Unused Mortar and Pestle", "description": "Clean. Empty. It has not seen use — at least, not recently."},
    "r4.png": {"name": "Spilled Herb Satchel", "description": "Dried leaves scattered across the floor. Common herbs, common carelessness."},
    "r5.png": {"name": "Broken Lantern with Ash", "description": "Its light went out long before it could reveal anything useful."},
    "r6.png": {"name": "Loose Stone from Castle Wall", "description": "Age loosens even the strongest foundations. Not every gap is deliberate."},
    "r7.png": {"name": "Smudged Ink Quill", "description": "Ink stains from countless hands. Words written here could belong to anyone."},
    "r8.png": {"name": "Unidentified Footprint in Wax", "description": "The shape is warped, the impression incomplete. Too much time has passed."},
    "r9.png": {"name": "Ornate Silver Wine Decanter", "description": "Handled by many. Trusted by all. Its surface remembers none of them."},
    "r10.png": {"name": "Royal Lineage Tapestry", "description": "Generations of rulers, stitched into fabric. Power outlasts people — at least, it tries to."},
    "r11.png": {"name": "Burned-Down Candle Stub", "description": "Time measured in wax and flame. Whatever happened here happened slowly — or too quickly."},
    "r12.png": {"name": "Daily Meal Ledger", "description": "A record of routine. Nothing stands out, and yet everything passed through these hands."},
}

# Killer-based replacements: (scene_0based_index, original_filename) -> replacement_filename
# Non-killer 5/10 point artifacts are swapped; replacement gives 0 points.
REPLACEMENT_MAP: dict = {
    (1, "c10-1.png"): "r3.png",
    (1, "q5-2.png"): "r1.png",
    (2, "g5-1.png"): "r6.png",
    (2, "q5-1.png"): "r2.png",
    (3, "c5-1.png"): "r4.png",
    (3, "g5-2.png"): "r5.png",
    (3, "q10-2.png"): "r7.png",
    (4, "g10-1.png"): "r8.png",
    (4, "c5-2.png"): "r9.png",
    (4, "q10-1.png"): "r10.png",
    (5, "c10-2.png"): "r11.png",
    (5, "g10-2.png"): "r12.png",
}

EVIDENCE_POINTS_REQUIRED = 12

# Opening cutscene text: one list per slide (game_op1 .. game_op7), each a list of text boxes.
# Each string is "SPEAKER\nDialogue" — speaker line is drawn above the quote in a smaller style.
OPENING_SCRIPT: List[List[str]] = [
    [  # OP1 — The Castle at Night
        "Narration\nFor three centuries, the Kingdom of Vaelor has known only one ruler.",
        "Narration\nKing Aldric the Everlasting.\nThe Immortal King.",
        "Narration\nIt is said no blade could wound him.\nNo poison could claim him.\nNo illness could weaken him.",
        "Narration\nTonight… that legend trembles.",
    ],
    [  # OP2 — The King in Bed, Family Mourning, Mage Casting
        "Narration\nThe King was found before dusk…",
        "Narration\nUnresponsive.",
        "Queen Elira\n\"He was well this morning…\"",
        "Princess Lyra\n\"Father… please wake…\"",
        "Archmage Seredin\n\"Something binds him. Not death… not yet.\"",
    ],
    [  # OP3 — Inquisitor and Knight Enter the Chamber
        "Narration\nYou are the High Inquisitor of Vaelor.",
        "Narration\nWhen treason threatens the crown, you uncover the truth.",
        "Knight-Captain Rowan\n\"The chamber was sealed. Only those within the castle walls had access.\"",
        "Narration\nThree stand within suspicion.",
    ],
    [  # OP4 — The Mage Explains the Ritual (Crystal Floating)
        "Archmage Seredin\n\"His life lingers… caught between breath and silence.\"",
        "Archmage Seredin\n\"If poison was used… its source may still echo within memory.\"",
        "Archmage Seredin\n\"Bring me proof of the guilty.\"",
        "Archmage Seredin\n\"Three fragments of truth.\"",
        "Archmage Seredin\n\"If the correct blood is taken… I may yet anchor him to this world.\"",
    ],
    [  # OP5 — Close-Up of Inquisitor
        "Narration\nYou have limited time.",
        "Narration\nThe King's memory fades.",
        "Narration\nEach chamber you enter holds fragments of truth… and falsehood.",
        "Narration\nChoose wisely.",
    ],
    [  # OP6 — Crystal Intensifies, Mage's Final Instruction
        "Archmage Seredin\n\"Select three pieces of evidence.\"",
        "Archmage Seredin\n\"Accuse the traitor.\"",
        "Archmage Seredin\n\"Justice will be swift.\"",
        "Archmage Seredin\n\"The King must endure.\"",
    ],
    [  # OP7 — Crystal Alone in Darkness (Foreshadow)
        "Narration\nLegends speak of immortality.",
        "Narration\nBut even legends rest upon fragile things.",
        "Narration\nAnd fragile things… crack.",
        "Narration\nThe investigation begins.",
    ],
]

# Bad Ending I — Truth Unclaimed: text boxes per scene (Speaker\nDialogue format, same as opening)
BAD_ENDING_RED_CRYSTAL: List[str] = [
    "Narration\nThe ritual begins.",
    "Archmage Selwyn\n\"The fragments… they are incomplete.\"",
    "Narration\nThe Mnemosyne Prism trembles.\n\nIts light shifts — not green… but red.",
    "Archmage Selwyn\n\"There is not enough truth to bind him.\"",
    "Narration\nThe crystal screams.",
    "Narration\nAnd then it fractures.",
]
BAD_ENDING_DEAD_KING: List[str] = [
    "Narration\nKing Aldric exhales.\n\nAnd does not breathe again.",
    "Queen Elira\n\"No…\"",
    "Prince\n\"Father…\"",
    "Archmage Selwyn\n\"The thread has severed.\"",
    "Narration\nThe Immortal King lies still.",
    "Narration\nLegends do not resist silence.",
]
BAD_ENDING_QUEEN_FREE: List[str] = [
    "Narration\nIn mourning black, Queen Elira stood before the court.",
    "Narration\nNo accusation touched her.\nNo proof condemned her.",
    "Narration\nThe crown passed quietly.",
    "Narration\nBehind every grieving smile…\nambition endured.",
    "Narration\nVaelor did not fall that night.\n\nBut something within it did.",
    "Narration\nYou failed to gather sufficient evidence.\nThe ritual collapsed.\nThe traitor remains upon the throne.",
    "Narration\nBAD ENDING I — Truth Unclaimed",
]
BAD_ENDING_GOBLIN_FREE: List[str] = [
    "Narration\nBrannic Ashhand kept his post.\n\nSweeping ash.\nStoking flame.",
    "Narration\nNo one questioned the hands that moved unseen through corridors.",
    "Narration\nNo one noticed the embers burning after midnight.",
    "Narration\nIn furnace light…\nhe smiled.",
    "Narration\nA kingdom may survive a dead king.\n\nIt may not survive a hidden spark.",
    "Narration\nYou failed to gather sufficient evidence.\nThe ritual collapsed.\nThe true traitor walks freely within Vaelor.",
    "Narration\nBAD ENDING I — Truth Unclaimed",
]
BAD_ENDING_CHEF_FREE: List[str] = [
    "Narration\nMaster Edrin Vale continued to prepare the royal meals.",
    "Narration\nHe bowed when addressed.\nHe smiled when thanked.",
    "Narration\nIn the kitchens of Vaelor, flavors masked many things.",
    "Narration\nTrust… among them.",
    "Narration\nThe blade that cuts bread may cut deeper still.",
    "Narration\nYou failed to gather sufficient evidence.\nThe ritual collapsed.\nThe poisoner remains among the living.",
    "Narration\nBAD ENDING I — Truth Unclaimed",
]

# Bad Ending II — The False Judgement (correct evidence ≥12, wrong accusation)
# Scene 7 — Blood on crystal turns red; Scene 8 — King dies; then culprit-free final scene
BAD_ENDING_II_RED_CRYSTAL: List[str] = [
    "Narration\nThe blood touches light.",
    "Narration\nFor a moment… it glows green.",
    "Narration\nThen the color fractures.",
    "Mage\n\"No…\"",
    "Narration\nThe crystal burns red.",
]
BAD_ENDING_II_DEAD_KING: List[str] = [
    "Narration\nThe light falters.",
    "Mage\n\"It does not bind.\"",
    "Narration\nThe King exhales.",
    "Narration\nAnd does not breathe again.",
    "Narration\nThe Immortal King… lies still.",
]
BAD_ENDING_II_QUEEN_FREE: List[str] = [
    "Narration\nThe Queen wept the loudest.",
    "Narration\nGrief can resemble innocence.",
    "Narration\nBut power wears many masks.",
    "Narration\nWith the throne unguarded… she claims what was always within reach.",
    "Narration\nVaelor bends to a quieter tyranny.",
    "Narration\nBAD ENDING II — The False Judgement",
]
BAD_ENDING_II_CHEF_FREE: List[str] = [
    "Narration\nThe kitchens never close.",
    "Narration\nAnd poison needs no throne.",
    "Narration\nWhile another suffered chains… the cook walked free.",
    "Narration\nThe city dines.",
    "Narration\nNot knowing what simmers beneath the lid.",
    "Narration\nBAD ENDING II — The False Judgement",
]
BAD_ENDING_II_GOBLIN_FREE: List[str] = [
    "Narration\nSmall hands leave quiet traces.",
    "Narration\nYou never saw them.",
    "Narration\nWhile another bled in chains… the alchemist slipped into the dark.",
    "Narration\nThe walls of Vaelor crumble from within.",
    "Narration\nAnd something watches from the hills.",
    "Narration\nBAD ENDING II — The False Judgement",
]

# Shared win path (Bad II + Good): G1 first green crystal through G8 (second green crystal is Good-only).
# Scene G1 — The Crystal Turns Green
WIN_PATH_G1_GREEN_CRYSTAL: List[str] = [
    "Narration\nThe fragments align.\n\nTruth, gathered.\n\nBlood, named.\n\nThe crystal answers.",
]
# Scene G2 — Inquisitor Presents the Evidence to the Mage
WIN_PATH_G2_OPTIONS: List[str] = [
    "Inquisitor\n\"These are the truths drawn from memory.\"",
    "Mage\n\"They resonate… strongly.\"",
    "Narration\nThe spell is ready.\n\nOnly the guilty blood remains.",
]
# Scene G3 — The First Two Suspects Brought in
WIN_PATH_G3_ESCORT: List[str] = [
    "Knight\n\"She is the third.\"",
    "Narration\nThree stand beneath suspicion.\n\nOne truth binds them all.",
]
# Scene G4 — The Accusation
WIN_PATH_G4_CULPRITS: List[str] = [
    "Inquisitor\n\"I name the traitor.\"",
]
# Scene G5 — Culprit Pleads (variant by who player accused: 1=goblin, 2=chef, 3=queen)
WIN_PATH_G5_QUEEN_PLEADS: List[str] = [
    "Queen Elira\n\"I did what was necessary… for the crown.\"",
    "Narration\nKnight restrains her.\n\nEven royalty bleeds.",
]
WIN_PATH_G5_CHEF_PLEADS: List[str] = [
    "Chef\n\"I served him my whole life…\"",
    "Narration\nKnight binds his wrists.\n\nLoyal hands can still falter.",
]
WIN_PATH_G5_GOBLIN_PLEADS: List[str] = [
    "Goblin\n\"No—no! I only tended fires!\"",
    "Narration\nKnight grips him firmly.\n\nAsh clings to more than stone.",
]
# Scene G6 — Blood Taken
WIN_PATH_G6_BLOOD_TAKEN: List[str] = [
    "Narration\nProof must answer magic.\n\nThe cost is drawn.",
]
# Scene G7 — Inquisitor Hands Blood to the Mage
WIN_PATH_G7_HANDS_TO_MAGE: List[str] = [
    "Inquisitor\n\"Let truth decide.\"",
    "Mage\n\"So it shall.\"",
]
# G8 (Blood poured onto crystal — second green) is in GOOD_ENDING_GREEN_CRYSTAL for Good path only.

# Good Ending — The Crown of the Mortal King (correct evidence ≥12, correct culprit)
# Four slides: green_cryst (G1+G8), dead_king (G10+G11), ge1 (G9), ge2 (G12+final title)
GOOD_ENDING_GREEN_CRYSTAL: List[str] = [
    "Narration\nThe fragments align.\n\nTruth, gathered.\n\nBlood, named.\n\nThe crystal answers.",
    "Narration\nThe chamber floods with light.\n\nHope returns in a single breath.",
]
GOOD_ENDING_DEAD_KING: List[str] = [
    "Narration\nThe glow fades.\n\nThe crystal hum weakens.\n\nThe King exhales.\n\nAnd does not inhale again.",
    "Mage\n\"The spell… held him only a moment.\"",
    "Narration\nEven light must dim.",
    "Narration\nThe Immortal King is laid to rest.\n\nThe crystal that once promised eternity\nnow bears a fracture of its own.\n\nNot shattered.\n\nBut changed.",
]
GOOD_ENDING_GE1_REJOICING: List[str] = [
    "Daughter\n\"He will live…\"",
    "Son\n\"Father…\"",
    "Narration\nKnight lowers his blade.\n\nQueen and family weep in relief.",
    "Narration\nFor a moment… the kingdom believes.",
]
GOOD_ENDING_GE2_CORONATION: List[str] = [
    "Narration\nThe Queen places the crown upon him.\n\nBut this time, no one speaks of forever.",
    "Inquisitor\n\"Our new king does not claim eternity.\"",
    "Mage\n\"He claims tomorrow.\"",
    "Narration\nHe will not be called The Everlasting.\n\nHe will not be called The Immortal King.\n\nHe will be known as—\n\nThe Mortal King.",
    "Narration\nGOOD ENDING:\n\"The Crown of the Mortal King\"",
    "Narration\nJustice was served.\n\nThe guilty were bound.\n\nThe kingdom did not fall.\n\nAnd though legends fade…\n\nPeople endure.",
]


def _parse_artifact_suspect_and_points(filename: str) -> Tuple[str, int]:
    """From artifact filename (e.g. q10-1.png, c5-2.png) return (suspect_id, points). r1.png -> ("", 0)."""
    base = filename.lower().replace(".png", "")
    if base.startswith("r") and base[1:].isdigit():
        return ("", 0)
    if not base or base[0] not in ("q", "c", "g"):
        return ("", 0)
    suspect = {"q": "queen", "c": "chef", "g": "goblin"}[base[0]]
    num_str = ""
    for c in base[1:]:
        if c.isdigit():
            num_str += c
        else:
            break
    points = int(num_str) if num_str else 0
    return (suspect, points)


@dataclass
class Snapshot:
    surface: pygame.Surface
    tags: List[str]
    scene_label: str
    points_queen: int = 0
    points_chef: int = 0
    points_goblin: int = 0
    trigger_artifact_filename: str | None = None  # artifact popup that triggered this snapshot, or None if via keyboard S


@dataclass
class Suspect:
    id: str
    name: str
    role: str
    motive: str
    requires: List[str]
    flavour: str


# ---------------------------------------------------------------------------
# Reusable draw helpers (procedural only)
# ---------------------------------------------------------------------------

def draw_vignette(surface: pygame.Surface, intensity: float = 0.6) -> None:
    """Darken screen edges with a soft vignette."""
    w, h = surface.get_size()
    cx, cy = w / 2, h / 2
    max_d = math.sqrt(cx * cx + cy * cy)
    for y in range(0, h, 4):
        for x in range(0, w, 4):
            d = math.sqrt((x - cx) ** 2 + (y - cy) ** 2) / max_d
            v = min(1.0, intensity * ease_out_quad(d))
            rect = pygame.Rect(x, y, 4, 4)
            s = surface.subsurface(rect).copy()
            s.fill((0, 0, 0, int(40 * v)))
            surface.blit(s, (x, y), special_flags=pygame.BLEND_RGBA_SUB)
    # Faster approximate: radial gradient overlay
    overlay = pygame.Surface((w, h), pygame.SRCALPHA)
    for radius in range(max(w, h) // 2, 0, -30):
        alpha = int(intensity * 80 * (1 - radius / (max(w, h) / 2)) ** 1.5)
        if alpha <= 0:
            break
        pygame.draw.circle(overlay, (0, 0, 0, min(255, alpha)), (int(cx), int(cy)), radius)
    surface.blit(overlay, (0, 0))


def draw_vignette_fast(surface: pygame.Surface, intensity: float = 0.5) -> None:
    """Faster vignette using a pre-drawn gradient."""
    w, h = surface.get_size()
    cx, cy = w // 2, h // 2
    r = max(w, h)
    overlay = pygame.Surface((w, h), pygame.SRCALPHA)
    steps = 8
    for i in range(steps, 0, -1):
        radius = r * (i / steps)
        alpha = int(intensity * 100 * (1 - (i / steps) ** 0.7))
        pygame.draw.circle(overlay, (0, 0, 0, min(255, alpha)), (cx, cy), int(radius))
    surface.blit(overlay, (0, 0))


def draw_glowing_circle(
    surface: pygame.Surface,
    center: Tuple[float, float],
    radius: float,
    base_color: Tuple[int, int, int],
    pulse: float,
    glow_radius_extra: float = 15,
) -> None:
    """Draw a circle with animated outer glow and pulse."""
    cx, cy = int(center[0]), int(center[1])
    # Outer glow layers
    for r_off in range(int(glow_radius_extra), 0, -3):
        alpha = int(40 * (1 - r_off / (glow_radius_extra + 1)) * (0.8 + 0.2 * pulse))
        s = pygame.Surface((radius * 2 + r_off * 4, radius * 2 + r_off * 4), pygame.SRCALPHA)
        pygame.draw.circle(
            s, (*base_color, alpha),
            (s.get_width() // 2, s.get_height() // 2),
            int(radius + r_off),
        )
        surface.blit(s, (cx - s.get_width() // 2, cy - s.get_height() // 2))
    # Main ring
    ring_thick = max(3, int(4 + pulse * 2))
    pygame.draw.circle(surface, base_color, (cx, cy), int(radius), ring_thick)
    # Inner dim fill
    inner = pygame.Surface((radius * 2, radius * 2), pygame.SRCALPHA)
    pygame.draw.circle(inner, (*base_color, 30), (int(radius), int(radius)), int(radius) - 4)
    surface.blit(inner, (cx - radius, cy - radius))


def draw_glitch_overlay(surface: pygame.Surface, amount: float, time: float) -> None:
    """Subtle scanline and horizontal shift glitch."""
    w, h = surface.get_size()
    # Scanlines
    scan = pygame.Surface((w, h), pygame.SRCALPHA)
    for y in range(0, h, 4):
        a = int(8 * amount * (0.5 + 0.5 * math.sin(time * 3 + y * 0.02)))
        pygame.draw.line(scan, (0, 0, 0, a), (0, y), (w, y))
    surface.blit(scan, (0, 0))
    # Occasional horizontal slice shift
    if amount > 0.3 and random.random() < 0.02:
        slice_h = random.randint(2, 15)
        sy = random.randint(0, h - slice_h)
        shift = random.randint(-4, 4)
        sub = surface.subsurface((0, sy, w, slice_h)).copy()
        surface.blit(sub, (shift, sy))
        surface.blit(sub, (-shift, sy + slice_h))


def draw_polaroid_frame(
    surface: pygame.Surface,
    rect: pygame.Rect,
    image: pygame.Surface,
    tilt: float = 0.0,
    shadow_offset: Tuple[int, int] = (6, 6),
) -> None:
    """Draw an image in a polaroid-style frame with shadow and tilt."""
    border = 10
    # Shadow
    shadow_surf = pygame.Surface((rect.w + 20, rect.h + 20), pygame.SRCALPHA)
    pygame.draw.rect(
        shadow_surf, (0, 0, 0, 80),
        (10, 10, rect.w, rect.h), border_radius=4,
    )
    if abs(tilt) > 0.01:
        shadow_surf = pygame.transform.rotate(shadow_surf, tilt * 0.5)
    shadow_pos = (rect.x - 10 + shadow_offset[0], rect.y - 10 + shadow_offset[1])
    surface.blit(shadow_surf, shadow_pos)
    # White border frame (polaroid)
    frame_surf = pygame.Surface((rect.w, rect.h), pygame.SRCALPHA)
    pygame.draw.rect(frame_surf, (255, 255, 255, 255), (0, 0, rect.w, rect.h), border_radius=3)
    img_inner = pygame.Rect(border, border, rect.w - 2 * border, rect.h - 2 * border)
    scaled = pygame.transform.smoothscale(image, img_inner.size)
    frame_surf.blit(scaled, (border, border))
    if abs(tilt) > 0.01:
        frame_surf = pygame.transform.rotate(frame_surf, tilt)
        frame_rect = frame_surf.get_rect(center=rect.center)
        surface.blit(frame_surf, frame_rect.topleft)
    else:
        surface.blit(frame_surf, (rect.x, rect.y))


def draw_noise_texture(surface: pygame.Surface, alpha: int, time: float) -> None:
    """Subtle animated noise overlay."""
    w, h = surface.get_size()
    noise = pygame.Surface((w, h), pygame.SRCALPHA)
    random.seed(int(time * 10) % 100000)
    for _ in range(min(2000, w * h // 50)):
        x, y = random.randint(0, w - 1), random.randint(0, h - 1)
        v = random.randint(0, alpha)
        noise.set_at((x, y), (255, 255, 255, v))
    surface.blit(noise, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)


# ---------------------------------------------------------------------------
# Game
# ---------------------------------------------------------------------------

class VanishingMemoriesGame:
    WIDTH = 1100
    HEIGHT = 700
    FPS = 60
    GLOBAL_TIME_LIMIT = 120.0  # 2 minutes; timer never pauses (runs in menu and in scene)
    MAX_SNAPSHOTS = 3
    CLOCK_RADIUS = 52
    SCENE_HEIGHT = int(700 * 0.72)
    # Clock grid: 3 columns, 2 rows, centered on screen
    CLOCK_SPACING = 200
    CLOCK_GRID_TOP = 260

    def __init__(self, screen: pygame.Surface) -> None:
        self.screen = screen
        pygame.display.set_caption("Truth Has a Half Life")
        self.clock = pygame.time.Clock()
        self._time_accum = 0.0

        # Procedural sounds and background music (Yakov Golman, Free Music Archive, CC BY)
        try:
            pygame.mixer.init(frequency=22050, size=-16, channels=1, buffer=512)
            self.sounds = _procedural_sounds()
            root = os.path.dirname(os.path.abspath(__file__))
            music_path = os.path.join(root, "background_track.mp3")
            if os.path.isfile(music_path):
                pygame.mixer.music.load(music_path)
                pygame.mixer.music.set_volume(0.5)
                pygame.mixer.music.play(loops=-1)
        except pygame.error:
            self.sounds = {}

        # Fonts (use defaults if no nice font)
        self.title_font = pygame.font.SysFont("arial", 42, bold=True)
        self.text_font = pygame.font.SysFont("arial", 20)
        self.small_font = pygame.font.SysFont("arial", 16)
        self.big_result_font = pygame.font.SysFont("arial", 64, bold=True)
        # Medieval popup: serif font if available (Times, Georgia, or system serif)
        for name in ("timesnewroman", "times new roman", "georgia", "serif"):
            try:
                self.popup_title_font = pygame.font.SysFont(name, 22, bold=True)
                self.popup_text_font = pygame.font.SysFont(name, 18)
                self.popup_small_font = pygame.font.SysFont(name, 15)
                break
            except Exception:
                continue
        else:
            self.popup_title_font = self.text_font
            self.popup_text_font = self.text_font
            self.popup_small_font = self.small_font

        self.suspects = self._build_suspects()
        self.culprit = random.choice(self.suspects)
        self.scenes = self._build_scenes(self.culprit.id)

        self.state = "opening"
        self.global_time = self.GLOBAL_TIME_LIMIT
        self.current_scene_index = -1
        self.snapshots: List[Snapshot] = []
        self.selected_suspect: Suspect | None = None
        self.result_message: str | None = None
        self.result_success: bool = False

        # Opening sequence (game_op1.png .. game_op7.png in open_scene folder)
        self.opening_images: List[pygame.Surface] = []
        self._load_opening_images()
        self.opening_slide_index = 0
        self.opening_timer = 0.0
        self.opening_phase = "fade_in"  # fade_in | holding | fade_out
        self.opening_text_index = 0  # which text box within current slide
        self.opening_char_index = 0  # how many chars shown (typing effect)
        self.opening_typing_accumulator = 0.0  # fractional chars (so typing works at 60fps)
        self.OPENING_FADE_IN_DURATION = 1.2
        self.OPENING_FADE_OUT_DURATION = 0.9
        self.OPENING_TYPING_CPS = 38  # characters per second

        # Clock menu art: menu.png background, c1.png–c6.png around center crystal
        self.menu_bg: pygame.Surface | None = None
        self.clock_images: List[pygame.Surface] = []
        self.clock_rects: List[pygame.Rect] = []  # click areas (centered on positions)
        self.clock_scene_descriptions = [
            "Scene 1 — Dawn Court (Great Hall)",
            "Scene 2 — The Royal Kitchens (Late Morning)",
            "Scene 3 — Inner Courtyard (Early Afternoon)",
            "Scene 4 — The Solar Chamber (Late Afternoon)",
            "Scene 5 — Private Supper (Evening)",
            "Scene 6 — The Bedchamber (Late Night)",
        ]
        self._load_menu_assets()

        # Animation state
        self.menu_time = 0.0
        self.scene_time = 0.0
        self.heartbeat_channel = None
        self.ominous_playing = False
        # Snapshot effect
        self.snapshot_freeze_surface: pygame.Surface | None = None
        self.snapshot_effect_time = 0.0
        self.snapshot_flash_alpha = 0
        # Camera shake
        self.camera_shake = (0.0, 0.0)
        self.camera_shake_decay = 0.92
        # Parallax
        self.parallax_offset = (0.0, 0.0)
        # Scene fade/dull: per-scene progress 0 = sharp, 1 = fully faded; continues when re-entering
        self.scene_fade_progress: List[float] = [0.0] * 6
        self.SCENE_FADE_DURATION = 42.0 / 3.0  # seconds until fully dulled (3x faster)
        # Hover for accuse
        self.hover_suspect_index = -1
        # Result animation
        self.result_time = 0.0
        # Artifact popup (when state == "artifact_popup")
        self.popup_artifact_filename: str = ""
        self.popup_scene_index: int = -1
        self.popup_artifact_surface: pygame.Surface | None = None  # larger version for popup, loaded on open

        # Ending sequence (state == "ending"): slides from end_scene folder, fade in/out like opening
        self.ending_slides: List[dict] = []  # list of {"image": str, "show_memories": bool, "accept_123": bool, "exit_prompt": bool}
        self.ending_index: int = 0
        self.ending_phase: str = "fade_in"  # fade_in | holding | fade_out
        self.ending_timer: float = 0.0
        self.ending_images: List[pygame.Surface] = []  # loaded surfaces for current run
        self.ending_player_choice: int = 0  # 1=goblin, 2=chef, 3=queen (set on the_culprits)
        self.ending_show_memories: bool = False  # hide after N_blood
        self.ending_memory_popup_index: int = -1  # which snapshot (0..n-1) popup is open; -1 = closed
        self.ending_text_index: int = 0  # which text box within current slide (when slide has "script")
        self.ending_char_index: int = 0
        self.ending_typing_accumulator: float = 0.0
        self.ENDING_FADE_IN = 1.2
        self.ENDING_FADE_OUT = 0.9
        self.ENDING_TYPING_CPS = 38
        self._ending_image_cache: dict = {}  # filename -> Surface, for current run

    def _load_ending_image(self, filename: str) -> pygame.Surface:
        """Load an image from end_scene folder and scale to (WIDTH, HEIGHT). Cache by filename."""
        if filename in self._ending_image_cache:
            return self._ending_image_cache[filename]
        root = os.path.dirname(os.path.abspath(__file__))
        # Script may be in project root (dihh) or inside end_scene; images live in end_scene/
        end_folder = os.path.join(root, "end_scene") if os.path.basename(root) != "end_scene" else root
        path = os.path.join(end_folder, filename)
        try:
            img = pygame.image.load(path).convert_alpha()
            surf = pygame.transform.smoothscale(img, (self.WIDTH, self.HEIGHT))
        except (pygame.error, FileNotFoundError):
            surf = pygame.Surface((self.WIDTH, self.HEIGHT))
            surf.fill((20, 22, 28))
        self._ending_image_cache[filename] = surf
        return surf

    def _start_ending(self) -> None:
        """Compute totals from snapshots; build loss or win slide list; set state to ending.
        Win = at least one suspect has >= EVIDENCE_POINTS_REQUIRED for that same suspect
        (only the killer can reach 12+ because non-killer 5/10 artifacts are replaced with 0-point items).
        """
        total_queen = sum(s.points_queen for s in self.snapshots)
        total_chef = sum(s.points_chef for s in self.snapshots)
        total_goblin = sum(s.points_goblin for s in self.snapshots)
        win = (
            total_queen >= EVIDENCE_POINTS_REQUIRED
            or total_chef >= EVIDENCE_POINTS_REQUIRED
            or total_goblin >= EVIDENCE_POINTS_REQUIRED
        )
        killer_id = self.culprit.id
        killer_be = {"goblin": "gob_be.png", "chef": "chef_be.png", "queen": "queen_be.png"}[killer_id]

        self._ending_image_cache.clear()
        self.ending_player_choice = 0
        self.ending_show_memories = False
        self.ending_memory_popup_index = -1

        if not win:
            culprit_script = {
                "queen": BAD_ENDING_QUEEN_FREE,
                "goblin": BAD_ENDING_GOBLIN_FREE,
                "chef": BAD_ENDING_CHEF_FREE,
            }[killer_id]
            self.ending_slides = [
                {"image": "red_cryst.png", "script": BAD_ENDING_RED_CRYSTAL},
                {"image": "dead_king.png", "script": BAD_ENDING_DEAD_KING},
                {"image": killer_be, "exit_prompt": True, "script": culprit_script},
            ]
        else:
            self.ending_slides = [
                {"image": "green_cryst.png", "script": WIN_PATH_G1_GREEN_CRYSTAL},
                {"image": "the_options.png", "show_memories": True, "script": WIN_PATH_G2_OPTIONS},
                {"image": "escort_in.png", "show_memories": True, "script": WIN_PATH_G3_ESCORT},
                {"image": "the_culprits.png", "show_memories": True, "accept_123": True, "script": WIN_PATH_G4_CULPRITS},
            ]
        self.ending_index = 0
        self.ending_phase = "fade_in"
        self.ending_timer = 0.0
        self.ending_text_index = 0
        self.ending_char_index = 0
        self.ending_typing_accumulator = 0.0
        self.state = "ending"

    def _ending_append_blood_and_continuation(self) -> None:
        """Called when user presses 1/2/3 on the_culprits: append blood slide (G5), blood_transfer (G6), mage_cooking (G7)."""
        blood_img = {1: "goblin_blood.png", 2: "chef_blood.png", 3: "queen_blood.png"}[self.ending_player_choice]
        g5_script = {1: WIN_PATH_G5_GOBLIN_PLEADS, 2: WIN_PATH_G5_CHEF_PLEADS, 3: WIN_PATH_G5_QUEEN_PLEADS}[self.ending_player_choice]
        self.ending_slides.append({"image": blood_img, "script": g5_script})
        self.ending_slides.append({"image": "blood_transfer.png", "script": WIN_PATH_G6_BLOOD_TAKEN})
        self.ending_slides.append({"image": "mage_cooking.png", "script": WIN_PATH_G7_HANDS_TO_MAGE})

    def _ending_append_outcome(self, correct: bool) -> None:
        """Append win or loss sequence after mage_cooking; last slide has exit_prompt."""
        killer_id = self.culprit.id
        killer_be = {"goblin": "gob_be.png", "chef": "chef_be.png", "queen": "queen_be.png"}[killer_id]
        if correct:
            self.ending_slides.append({"image": "green_cryst.png", "script": GOOD_ENDING_GREEN_CRYSTAL})
            self.ending_slides.append({"image": "dead_king.png", "script": GOOD_ENDING_DEAD_KING})
            self.ending_slides.append({"image": "ge1.png", "script": GOOD_ENDING_GE1_REJOICING})
            self.ending_slides.append({"image": "ge2.png", "exit_prompt": True, "script": GOOD_ENDING_GE2_CORONATION})
        else:
            # Bad Ending II — The False Judgement (wrong accusation, with script)
            culprit_script_ii = {
                "queen": BAD_ENDING_II_QUEEN_FREE,
                "chef": BAD_ENDING_II_CHEF_FREE,
                "goblin": BAD_ENDING_II_GOBLIN_FREE,
            }[killer_id]
            self.ending_slides.append({"image": "red_cryst.png", "script": BAD_ENDING_II_RED_CRYSTAL})
            self.ending_slides.append({"image": "dead_king.png", "script": BAD_ENDING_II_DEAD_KING})
            self.ending_slides.append({"image": killer_be, "exit_prompt": True, "script": culprit_script_ii})

    def _build_scenes(self, killer_id: str) -> List[MemoryScene]:
        scenes = []
        w, h = self.WIDTH, self.SCENE_HEIGHT
        root = os.path.dirname(os.path.abspath(__file__))
        scenes_folder = os.path.join(root, "scenes")
        replacements_folder = os.path.join(root, "replacements")
        labels = ["09:12", "11:17", "12:03", "14:40", "18:22", "21:10"]
        # (filename, frac_x, frac_y, rotation_deg, darken [, scale [, offset_x_aw [, offset_y_ah ]]); max_side 80 * scale
        artifact_specs = [
            [("q1-1.png", 0.92, 0.5 + 1 / 8 + 0.08, 0, 0.48), ("c1-1.png", 1 / 16, 0.5, 0, 1.0, 0.75), ("g1-1.png", 0.45, 0.5, 0, 1.0, 1 / 3, 1.0, 0)],
            [("q5-2.png", 0.4, 0.5 + 3 / 16, 8, 1.0), ("c10-1.png", 0.42, 0.5, 0, 0.6, 1.0, 1.0, 0), ("g1-2.png", 5 / 6, 0.25, 0, 1.0, 2 / 3, -0.25, 0)],
            [("q5-1.png", 7 / 8, 7 / 8, 0, 0.35), ("c1-2.png", 0.45, 0.52, 0, 1.0, 1 / 3), ("g5-1.png", 3 / 4, 0.5, 0, 1.0, 0.5, 0.5, 0.25)],
            [("q10-2.png", 1 / 8, 7 / 8, 0, 0.35), ("c5-1.png", 1.0, 2 / 3, 0, 0.35, 2 / 3, -0.5, 0), ("g5-2.png", 3 / 8, 0.52, 0, 1.0)],
            [("q10-1.png", 0.92, 0.5 - 1 / 16, 0, 1.0), ("c5-2.png", 0.25, 0.98, 0, 0.6, 2.0, 0.5, 0), ("g10-1.png", 1 / 4, 0.48, 0, 0.85, 1.0, -0.25, -1.0)],
            [("q1-2.png", 0.93, 0.58, 0, 0.4), ("c10-2.png", 1 / 3, 1 / 3 - 0.06, 0, 0.35, 1.2, 0.5, 1.0), ("g10-2.png", 1 / 4, 0.5, 0, 0.5, 2 / 3, 0, -1 / 6)],
        ]
        tag_options = [
            ["dna", "time", "access"], ["jealousy", "relationship", "motive"], ["workshop", "insider", "struggle"],
            ["digital", "lure", "premeditation"], ["poison", "escape", "alibi_break"], ["entry", "forensics", "tools"],
        ]
        for i, label in enumerate(labels):
            subfolder = f"s{i + 1}"
            path = os.path.join(scenes_folder, subfolder, f"s{i + 1}.png")
            ox, oy, bw, bh = 0, 0, w, h
            try:
                img = pygame.image.load(path).convert()
                iw, ih = img.get_width(), img.get_height()
                scale = min(w / iw, h / ih)
                new_w = max(1, int(iw * scale))
                new_h = max(1, int(ih * scale))
                scaled = pygame.transform.smoothscale(img, (new_w, new_h))
                ox, oy = (w - new_w) // 2, (h - new_h) // 2
                bw, bh = new_w, new_h
                bg = pygame.Surface((w, h))
                bg.fill((28, 30, 38))
                bg.blit(scaled, (ox, oy))
            except (pygame.error, FileNotFoundError):
                bg = pygame.Surface((w, h))
                for y in range(h):
                    t = y / h
                    pygame.draw.line(bg, (int(55 + 30 * (1 - t)), int(62 + 35 * (1 - t)), int(78 + 35 * (1 - t))), (0, y), (w, y))
                ox, oy, bw, bh = 0, 0, w, h
            artifacts = []
            for spec in artifact_specs[i]:
                orig_filename = spec[0]
                suspect_id, points = _parse_artifact_suspect_and_points(orig_filename)
                use_replacement = (
                    killer_id != suspect_id
                    and points in (5, 10)
                    and (i, orig_filename) in REPLACEMENT_MAP
                )
                if use_replacement:
                    load_filename = REPLACEMENT_MAP[(i, orig_filename)]
                    art_path = os.path.join(replacements_folder, load_filename)
                    display_filename = load_filename
                    points = 0
                    suspect_id = ""
                else:
                    art_path = os.path.join(scenes_folder, subfolder, orig_filename)
                    display_filename = orig_filename
                try:
                    art_img = pygame.image.load(art_path).convert_alpha()
                    scale_spec = spec[5] if len(spec) > 5 else 1.0
                    max_side = max(1, int(80 * scale_spec))
                    aw, ah = art_img.get_width(), art_img.get_height()
                    if aw > ah:
                        if aw > max_side:
                            ah = max(1, int(ah * max_side / aw))
                            aw = max_side
                    else:
                        if ah > max_side:
                            aw = max(1, int(aw * max_side / ah))
                            ah = max_side
                    art_img = pygame.transform.smoothscale(art_img, (aw, ah))
                    if spec[3] != 0:
                        art_img = pygame.transform.rotate(art_img, spec[3])
                    off_x = spec[6] if len(spec) > 6 else 0.0
                    off_y = spec[7] if len(spec) > 7 else 0.0
                    artifacts.append(SceneArtifact(
                        surface=art_img, frac_x=spec[1], frac_y=spec[2], tags=tag_options[i].copy(),
                        rotation_degrees=spec[3], darken=spec[4], offset_x_aw=off_x, offset_y_ah=off_y,
                        spec_filename=display_filename, suspect_id=suspect_id, points=points
                    ))
                except (pygame.error, FileNotFoundError):
                    pass
            scenes.append(MemoryScene(label=label, background=bg, bg_rect=(ox, oy, bw, bh), artifacts=artifacts))
        return scenes

    def _build_suspects(self) -> List[Suspect]:
        return [
            Suspect("queen", "Queen Elira", "Queen", "Power & Succession", [], "The crown weighs more than it appears."),
            Suspect("goblin", "Brannic Ashhand", "Goblin Groundskeeper", "Resentment & Access", [], "His hands are worn from work no one sees."),
            Suspect("chef", "Edrin Vale", "Royal Chef", "Loyalty & Poison", [], "He has fed three kings. Trust is another matter."),
        ]

    def _load_opening_images(self) -> None:
        """Load game_op1.png .. game_op7.png from open_scene folder (next to script)."""
        root = os.path.dirname(os.path.abspath(__file__))
        folder = os.path.join(root, "open_scene")
        for i in range(1, 8):
            path = os.path.join(folder, f"game_op{i}.png")
            try:
                img = pygame.image.load(path).convert()
                # Scale to fill screen (convert so set_alpha works for fade-in)
                img = pygame.transform.smoothscale(img, (self.WIDTH, self.HEIGHT))
                self.opening_images.append(img)
            except (pygame.error, FileNotFoundError):
                # Placeholder: dark surface so sequence still runs
                surf = pygame.Surface((self.WIDTH, self.HEIGHT))
                surf.fill((20, 22, 28))
                self.opening_images.append(surf)
        if not self.opening_images:
            self.opening_images.append(pygame.Surface((self.WIDTH, self.HEIGHT)))
            self.opening_images[0].fill((0, 0, 0))

    def _load_menu_assets(self) -> None:
        """Load menu.png and c1.png–c6.png from menu_pics folder. Clocks arranged around center crystal."""
        root = os.path.dirname(os.path.abspath(__file__))
        menu_folder = os.path.join(root, "menu_pics")
        try:
            bg = pygame.image.load(os.path.join(menu_folder, "menu.png")).convert()
            self.menu_bg = pygame.transform.smoothscale(bg, (self.WIDTH, self.HEIGHT))
        except (pygame.error, FileNotFoundError):
            self.menu_bg = None
        cx, cy = self.WIDTH // 2, self.HEIGHT // 2
        # Positions around crystal, pushed toward edges (c1 top-left … c6 bottom-right)
        offsets = [
            (-160, -240),   # c1 top, left
            (160, -240),    # c2 top, right
            (-340, 0),      # c3 middle left
            (340, 0),       # c4 middle right
            (-160, 240),    # c5 bottom, left
            (160, 240),     # c6 bottom, right
        ]
        # Left clocks (0,2,4) tilt left; right (1,3,5) tilt right (degrees)
        tilts = [-6, 6, -6, 6, -6, 6]
        scale = 1 / 3  # clocks at one-third size
        for i in range(1, 7):
            path = os.path.join(menu_folder, f"c{i}.png")
            try:
                img = pygame.image.load(path).convert_alpha()
                w, h = max(1, int(img.get_width() * scale)), max(1, int(img.get_height() * scale))
                img = pygame.transform.smoothscale(img, (w, h))
                img = pygame.transform.rotate(img, tilts[i - 1])
                self.clock_images.append(img)
                px, py = cx + offsets[i - 1][0], cy + offsets[i - 1][1]
                rect = img.get_rect(center=(px, py))
                self.clock_rects.append(rect)
            except (pygame.error, FileNotFoundError):
                self.clock_images.append(pygame.Surface((1, 1)))
                self.clock_rects.append(pygame.Rect(0, 0, 0, 0))

    def run(self) -> None:
        running = True
        while running:
            dt = self.clock.tick(self.FPS) / 1000.0
            self._time_accum += dt

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        if self.state == "opening":
                            self.state = "menu"
                        elif self.state == "scene":
                            self.state = "menu"
                            self.current_scene_index = -1
                            if self.ominous_playing and self.sounds:
                                self.sounds.get("ominous", pygame.mixer.Sound()).stop()
                                self.ominous_playing = False
                        elif self.state == "menu":
                            running = False
                        elif self.state == "ending":
                            pass  # ESC does nothing in ending
                    elif self.state == "ending":
                        if self.ending_index < len(self.ending_slides):
                            slide = self.ending_slides[self.ending_index]
                            script = slide.get("script", [])
                            # Exit prompt: only quit when no script or script fully shown
                            if slide.get("exit_prompt") and (not script or self.ending_text_index >= len(script)):
                                running = False
                            elif slide.get("accept_123"):
                                if event.key == pygame.K_1:
                                    self.ending_player_choice = 1
                                    self._ending_append_blood_and_continuation()
                                    self.ending_index += 1
                                    self.ending_phase = "fade_in"
                                    self.ending_timer = 0.0
                                    self.ending_text_index = 0
                                    self.ending_char_index = 0
                                    self.ending_typing_accumulator = 0.0
                                elif event.key == pygame.K_2:
                                    self.ending_player_choice = 2
                                    self._ending_append_blood_and_continuation()
                                    self.ending_index += 1
                                    self.ending_phase = "fade_in"
                                    self.ending_timer = 0.0
                                    self.ending_text_index = 0
                                    self.ending_char_index = 0
                                    self.ending_typing_accumulator = 0.0
                                elif event.key == pygame.K_3:
                                    self.ending_player_choice = 3
                                    self._ending_append_blood_and_continuation()
                                    self.ending_index += 1
                                    self.ending_phase = "fade_in"
                                    self.ending_timer = 0.0
                                    self.ending_text_index = 0
                                    self.ending_char_index = 0
                                    self.ending_typing_accumulator = 0.0
                            elif self.ending_phase == "holding" and event.key == pygame.K_RIGHT:
                                if script and self.ending_text_index < len(script):
                                    full_text = script[self.ending_text_index]
                                    if self.ending_char_index < len(full_text):
                                        self.ending_char_index = len(full_text)
                                    if self.ending_char_index >= len(full_text):
                                        if self.ending_text_index + 1 < len(script):
                                            self.ending_text_index += 1
                                            self.ending_char_index = 0
                                            self.ending_typing_accumulator = 0.0
                                        elif slide.get("exit_prompt"):
                                            self.ending_text_index = len(script)  # show exit prompt
                                        else:
                                            self.ending_phase = "fade_out"
                                            self.ending_timer = 0.0
                                else:
                                    self.ending_phase = "fade_out"
                                    self.ending_timer = 0.0
                        else:
                            if event.key in (pygame.K_RIGHT, pygame.K_SPACE, pygame.K_RETURN):
                                pass
                    elif self.state == "opening" and event.key == pygame.K_RIGHT:
                        if self.opening_phase == "holding":
                            slide_idx = self.opening_slide_index
                            script = OPENING_SCRIPT[slide_idx] if slide_idx < len(OPENING_SCRIPT) else []
                            if self.opening_text_index < len(script):
                                full_text = script[self.opening_text_index]
                                if self.opening_char_index < len(full_text):
                                    self.opening_char_index = len(full_text)
                                # If current box is fully shown, advance (same press or already complete)
                                if self.opening_char_index >= len(full_text):
                                    if self.opening_text_index + 1 < len(script):
                                        self.opening_text_index += 1
                                        self.opening_char_index = 0
                                        self.opening_typing_accumulator = 0.0
                                    else:
                                        self.opening_phase = "fade_out"
                                        self.opening_timer = 0.0
                            else:
                                self.opening_phase = "fade_out"
                                self.opening_timer = 0.0
                    elif self.state == "scene" and event.key == pygame.K_s:
                        self._take_snapshot()
                    elif self.state == "artifact_popup" and event.key == pygame.K_ESCAPE:
                        self._close_artifact_popup()
                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    if self.state == "menu":
                        self._handle_menu_click(event.pos)
                    elif self.state == "scene":
                        self._handle_scene_click(event.pos)
                    elif self.state == "artifact_popup":
                        self._handle_artifact_popup_click(event.pos)
                    elif self.state == "ending":
                        if self.ending_index < len(self.ending_slides):
                            slide = self.ending_slides[self.ending_index]
                            script = slide.get("script", [])
                            if slide.get("exit_prompt") and (not script or self.ending_text_index >= len(script)):
                                running = False
                                continue
                        if self.ending_memory_popup_index >= 0:
                            box_w, box_h = 520, 380
                            box_x = (self.WIDTH - box_w) // 2
                            box_y = (self.HEIGHT - box_h) // 2
                            x_btn = pygame.Rect(box_x + box_w - 36, box_y + 8, 28, 28)
                            close_hint_surf = self.popup_small_font.render("Close (X)", True, (165, 145, 110))
                            close_text_rect = pygame.Rect(
                                box_x + box_w - 24 - close_hint_surf.get_width(),
                                box_y + box_h - 36,
                                close_hint_surf.get_width(),
                                close_hint_surf.get_height(),
                            )
                            if x_btn.collidepoint(event.pos) or close_text_rect.collidepoint(event.pos) or not pygame.Rect(box_x, box_y, box_w, box_h).collidepoint(event.pos):
                                self._close_ending_memory_popup()
                            continue
                        self._handle_ending_memory_click(event.pos)

            if self.state == "opening":
                self._update_opening(dt)
            elif self.state == "menu":
                self._update_global_timer(dt)
                self.menu_time += dt
            elif self.state == "snapshot_effect":
                self._update_snapshot_effect(dt)
            elif self.state == "scene":
                self._update_global_timer(dt)
                self._update_scene(dt)
            elif self.state == "artifact_popup":
                self._update_global_timer(dt)
            elif self.state == "ending":
                self._update_ending(dt)

            if self.state == "opening":
                self.draw_opening()
            elif self.state in ("menu", "snapshot_effect"):
                self.draw_menu()
            elif self.state == "scene":
                self.draw_scene()
            elif self.state == "artifact_popup":
                self.draw_scene()
                self._draw_artifact_popup()
            elif self.state == "ending":
                self.draw_ending()
            # Cursor: pointer over popup buttons when in artifact_popup; scene sets pointer over artifacts
            try:
                if self.state == "artifact_popup":
                    mx, my = pygame.mouse.get_pos()
                    box_w, box_h = 520, 440
                    box_x = (self.WIDTH - box_w) // 2
                    box_y = (self.SCENE_HEIGHT - box_h) // 2
                    x_btn = pygame.Rect(box_x + box_w - 36, box_y + 8, 28, 28)
                    btn_y, btn_h = box_y + box_h - 56, 40
                    cryst_btn = pygame.Rect(box_x + 24, btn_y, 160, btn_h)
                    uncryst_btn = pygame.Rect(box_x + 24 + 164, btn_y, 130, btn_h)
                    close_btn = pygame.Rect(box_x + box_w - 24 - 100, btn_y, 100, btn_h)
                    if x_btn.collidepoint(mx, my) or cryst_btn.collidepoint(mx, my) or uncryst_btn.collidepoint(mx, my) or close_btn.collidepoint(mx, my):
                        pygame.mouse.set_cursor(pygame.cursors.Cursor(pygame.SYSTEM_CURSOR_HAND))
                    else:
                        pygame.mouse.set_cursor(pygame.cursors.Cursor(pygame.SYSTEM_CURSOR_ARROW))
                elif self.state not in ("scene", "artifact_popup"):
                    pygame.mouse.set_cursor(pygame.cursors.Cursor(pygame.SYSTEM_CURSOR_ARROW))
            except (AttributeError, TypeError):
                pass

            pygame.display.flip()
        pygame.quit()

    def _update_opening(self, dt: float) -> None:
        self.opening_timer += dt
        if self.opening_phase == "fade_in":
            if self.opening_timer >= self.OPENING_FADE_IN_DURATION:
                self.opening_phase = "holding"
                self.opening_timer = 0.0
                self.opening_text_index = 0
                self.opening_char_index = 0
                self.opening_typing_accumulator = 0.0
        elif self.opening_phase == "holding":
            # Advance typing effect (use accumulator so we get at least 1 char per frame when needed)
            if self.opening_slide_index < len(OPENING_SCRIPT) and self.opening_text_index < len(OPENING_SCRIPT[self.opening_slide_index]):
                full_text = OPENING_SCRIPT[self.opening_slide_index][self.opening_text_index]
                self.opening_typing_accumulator += dt * self.OPENING_TYPING_CPS
                while self.opening_typing_accumulator >= 1.0 and self.opening_char_index < len(full_text):
                    self.opening_char_index += 1
                    self.opening_typing_accumulator -= 1.0
                if self.opening_char_index >= len(full_text):
                    self.opening_typing_accumulator = 0.0
        elif self.opening_phase == "fade_out":
            if self.opening_timer >= self.OPENING_FADE_OUT_DURATION:
                self.opening_slide_index += 1
                self.opening_timer = 0.0
                if self.opening_slide_index >= len(self.opening_images):
                    self.state = "menu"
                    self.opening_slide_index = 0
                else:
                    self.opening_phase = "fade_in"

    def _update_ending(self, dt: float) -> None:
        """Fade in/out and advance slides; after mage_cooking append win/loss outcome."""
        self.ending_timer += dt
        if self.ending_phase == "fade_in":
            if self.ending_timer >= self.ENDING_FADE_IN:
                self.ending_phase = "holding"
                self.ending_timer = 0.0
                self.ending_text_index = 0
                self.ending_char_index = 0
                self.ending_typing_accumulator = 0.0
        elif self.ending_phase == "holding":
            slide = self.ending_slides[self.ending_index] if self.ending_index < len(self.ending_slides) else {}
            script = slide.get("script", [])
            if script and self.ending_text_index < len(script):
                full_text = script[self.ending_text_index]
                self.ending_typing_accumulator += dt * self.ENDING_TYPING_CPS
                while self.ending_typing_accumulator >= 1.0 and self.ending_char_index < len(full_text):
                    self.ending_char_index += 1
                    self.ending_typing_accumulator -= 1.0
                if self.ending_char_index >= len(full_text):
                    self.ending_typing_accumulator = 0.0
        elif self.ending_phase == "fade_out":
            if self.ending_timer >= self.ENDING_FADE_OUT:
                prev_index = self.ending_index
                self.ending_index += 1
                if prev_index < len(self.ending_slides) and self.ending_slides[prev_index].get("image") == "mage_cooking.png":
                    correct = (
                        (self.ending_player_choice == 1 and self.culprit.id == "goblin")
                        or (self.ending_player_choice == 2 and self.culprit.id == "chef")
                        or (self.ending_player_choice == 3 and self.culprit.id == "queen")
                    )
                    self._ending_append_outcome(correct)
                self.ending_timer = 0.0
                # Reset script state for the new slide so previous slide's text is never shown again
                self.ending_text_index = 0
                self.ending_char_index = 0
                self.ending_typing_accumulator = 0.0
                if self.ending_index < len(self.ending_slides):
                    self.ending_phase = "fade_in"
                else:
                    self.ending_phase = "holding"  # stay on last (should not happen; last has exit_prompt)

    def _update_global_timer(self, dt: float) -> None:
        """Decrement timer (runs in menu and in scene). When time runs out, go to ending (no accusation screen)."""
        if self.global_time > 0.0:
            self.global_time = max(0.0, self.global_time - dt)
            if self.global_time == 0.0:
                self._start_ending()
                self.current_scene_index = -1
                if self.ominous_playing and self.sounds:
                    self.sounds["ominous"].stop()
                    self.ominous_playing = False
        if len(self.snapshots) >= self.MAX_SNAPSHOTS and self.state == "menu":
            self._start_ending()

    def _clock_center(self, idx: int) -> Tuple[int, int]:
        """Get screen position of clock index (0-5). Grid centered on screen."""
        col, row = idx % 3, idx // 3
        cx = self.WIDTH // 2 + (col - 1) * self.CLOCK_SPACING
        cy = self.CLOCK_GRID_TOP + row * self.CLOCK_SPACING
        return (cx, cy)

    def _handle_menu_click(self, pos: Tuple[int, int]) -> None:
        # Accuse button (bottom-right): go to ending (no accusation screen)
        accuse_rect = pygame.Rect(self.WIDTH - 200, self.HEIGHT - 56, 180, 42)
        if accuse_rect.collidepoint(pos):
            self._start_ending()
            return
        if self.global_time <= 0.0:
            return
        # Clock hit test: use image rects if we have 6 clock assets, else legacy circle grid
        if len(self.clock_rects) >= 6 and all(self.clock_rects[i].width > 0 for i in range(6)):
            for idx, rect in enumerate(self.clock_rects):
                if rect.collidepoint(pos):
                    self.current_scene_index = idx
                    self.scene_time = 0.0
                    self.state = "scene"
                    break
        else:
            for idx, scene in enumerate(self.scenes):
                cx, cy = self._clock_center(idx)
                if (pos[0] - cx) ** 2 + (pos[1] - cy) ** 2 <= self.CLOCK_RADIUS ** 2:
                    self.current_scene_index = idx
                    self.scene_time = 0.0
                    self.state = "scene"
                    break

    def _get_artifact_index_at_pos(self, pos: Tuple[int, int]) -> int:
        """Return index of artifact under pos in current scene, or -1. Uses same rect logic as draw_scene."""
        if self.current_scene_index < 0:
            return -1
        scene = self.scenes[self.current_scene_index]
        ox, oy, bw, bh = scene.bg_rect
        mx, my = pos
        shake_x = int(self.camera_shake[0])
        shake_y = int(self.camera_shake[1])
        for i, art in enumerate(scene.artifacts):
            cx = ox + int(art.frac_x * bw)
            cy = oy + int(art.frac_y * bh)
            sw, sh = art.surface.get_width(), art.surface.get_height()
            dx = cx - sw // 2 + shake_x + int(art.offset_x_aw * sw)
            dy = cy - sh // 2 + shake_y + int(art.offset_y_ah * sh)
            r = pygame.Rect(dx, dy, sw, sh)
            if r.collidepoint(mx, my):
                return i
        return -1

    def _handle_scene_click(self, pos: Tuple[int, int]) -> None:
        idx = self._get_artifact_index_at_pos(pos)
        if idx < 0:
            return
        scene = self.scenes[self.current_scene_index]
        art = scene.artifacts[idx]
        if not art.spec_filename:
            return
        info = ARTIFACT_INFO.get(art.spec_filename)
        if not info:
            return
        self.popup_artifact_filename = art.spec_filename
        self.popup_scene_index = self.current_scene_index
        self._load_popup_artifact_image()
        self.state = "artifact_popup"

    def _close_artifact_popup(self) -> None:
        """Return to scene and clear popup state including cached image."""
        self.state = "scene"
        self.popup_artifact_filename = ""
        self.popup_scene_index = -1
        self.popup_artifact_surface = None

    def _load_popup_artifact_image(self) -> None:
        """Load a larger version of the artifact image for the popup. Clears previous if any."""
        self.popup_artifact_surface = None
        if self.popup_scene_index < 0 or not self.popup_artifact_filename:
            return
        root = os.path.dirname(os.path.abspath(__file__))
        fn = self.popup_artifact_filename.lower()
        if fn.startswith("r") and fn.endswith(".png") and fn[1:-4].isdigit():
            path = os.path.join(root, "replacements", self.popup_artifact_filename)
        else:
            subfolder = f"s{self.popup_scene_index + 1}"
            path = os.path.join(root, "scenes", subfolder, self.popup_artifact_filename)
        try:
            img = pygame.image.load(path).convert_alpha()
            max_side = 200
            w, h = img.get_width(), img.get_height()
            if w > h:
                if w > max_side:
                    h = max(1, int(h * max_side / w))
                    w = max_side
            else:
                if h > max_side:
                    w = max(1, int(w * max_side / h))
                    h = max_side
            self.popup_artifact_surface = pygame.transform.smoothscale(img, (w, h))
        except (pygame.error, FileNotFoundError):
            pass

    def _handle_artifact_popup_click(self, pos: Tuple[int, int]) -> None:
        # Popup layout: same rects as _draw_artifact_popup (box_h 440)
        box_w, box_h = 520, 440
        box_x = (self.WIDTH - box_w) // 2
        box_y = (self.SCENE_HEIGHT - box_h) // 2
        x_btn = pygame.Rect(box_x + box_w - 36, box_y + 8, 28, 28)
        if x_btn.collidepoint(pos):
            self._close_artifact_popup()
            return
        scene = self.scenes[self.popup_scene_index] if self.popup_scene_index >= 0 else None
        # Uncrystallize only from the same artifact that triggered the snapshot (or if snapshot was from keyboard S)
        can_uncrystallize_here = bool(
            scene and any(
                s.scene_label == scene.label and (s.trigger_artifact_filename is None or s.trigger_artifact_filename == self.popup_artifact_filename)
                for s in self.snapshots
            )
        )
        btn_y = box_y + box_h - 56
        btn_h = 40
        cryst_btn = pygame.Rect(box_x + 24, btn_y, 160, btn_h)
        uncryst_btn = pygame.Rect(box_x + 24 + 164, btn_y, 130, btn_h)
        close_btn = pygame.Rect(box_x + box_w - 24 - 100, btn_y, 100, btn_h)
        if cryst_btn.collidepoint(pos):
            if len(self.snapshots) < self.MAX_SNAPSHOTS:
                self._take_snapshot_from_popup()
                self.popup_artifact_filename = ""
                self.popup_scene_index = -1
                self.popup_artifact_surface = None
            return
        if uncryst_btn.collidepoint(pos) and can_uncrystallize_here:
            for i in range(len(self.snapshots) - 1, -1, -1):
                if self.snapshots[i].scene_label == scene.label:
                    self.snapshots.pop(i)
                    break
            self._close_artifact_popup()
            return
        if close_btn.collidepoint(pos):
            self._close_artifact_popup()
            return
        if not pygame.Rect(box_x, box_y, box_w, box_h).collidepoint(pos):
            self._close_artifact_popup()

    def _take_snapshot_from_popup(self) -> None:
        """Take a snapshot from the artifact popup. Only the clicked artifact's points count (not the whole scene)."""
        if len(self.snapshots) >= self.MAX_SNAPSHOTS:
            return
        if self.popup_scene_index < 0:
            return
        scene_h = self.SCENE_HEIGHT
        self.snapshot_freeze_surface = pygame.Surface((self.WIDTH, scene_h))
        self.snapshot_freeze_surface.blit(self.screen, (0, 0), (0, 0, self.WIDTH, scene_h))
        self.snapshot_effect_time = 0.0
        self.snapshot_flash_alpha = 255
        if self.sounds:
            self.sounds["snapshot"].play()
        scene = self.scenes[self.popup_scene_index]
        p_queen = p_chef = p_goblin = 0
        captured_tags = []
        for art in scene.artifacts:
            if art.spec_filename != self.popup_artifact_filename:
                continue
            if art.suspect_id == "queen":
                p_queen = art.points
            elif art.suspect_id == "chef":
                p_chef = art.points
            elif art.suspect_id == "goblin":
                p_goblin = art.points
            captured_tags = list(art.tags)
            break
        snap_surf = self.snapshot_freeze_surface.copy()
        self.snapshots.append(
            Snapshot(surface=snap_surf, tags=captured_tags, scene_label=scene.label, points_queen=p_queen, points_chef=p_chef, points_goblin=p_goblin, trigger_artifact_filename=self.popup_artifact_filename)
        )
        self.state = "snapshot_effect"

    def _update_scene(self, dt: float) -> None:
        if self.current_scene_index < 0:
            return
        self.scene_time += dt
        if self.current_scene_index >= 0 and self.current_scene_index < len(self.scene_fade_progress):
            self.scene_fade_progress[self.current_scene_index] = min(
                1.0, self.scene_fade_progress[self.current_scene_index] + dt / self.SCENE_FADE_DURATION
            )
        scene = self.scenes[self.current_scene_index]
        # Parallax
        self.parallax_offset = (
            math.sin(self.scene_time * 0.15) * 4,
            math.sin(self.scene_time * 0.12) * 3,
        )
        # Camera shake when time is low
        if self.global_time < 25 and self.global_time > 0:
            self.camera_shake = (
                self.camera_shake[0] + random.uniform(-2, 2),
                self.camera_shake[1] + random.uniform(-2, 2),
            )
            if not self.ominous_playing and self.sounds:
                self.sounds["ominous"].play(loops=-1)
                self.ominous_playing = True
        else:
            if self.ominous_playing and self.sounds:
                self.sounds["ominous"].stop()
                self.ominous_playing = False
        self.camera_shake = (
            self.camera_shake[0] * self.camera_shake_decay,
            self.camera_shake[1] * self.camera_shake_decay,
        )

    def _take_snapshot(self) -> None:
        if len(self.snapshots) >= self.MAX_SNAPSHOTS:
            return
        scene_h = self.SCENE_HEIGHT
        self.snapshot_freeze_surface = pygame.Surface((self.WIDTH, scene_h))
        self.snapshot_freeze_surface.blit(self.screen, (0, 0), (0, 0, self.WIDTH, scene_h))
        self.snapshot_effect_time = 0.0
        self.snapshot_flash_alpha = 255
        if self.sounds:
            self.sounds["snapshot"].play()
        scene = self.scenes[self.current_scene_index]
        captured_tags = []
        p_queen = p_chef = p_goblin = 0
        for art in scene.artifacts:
            captured_tags.extend(art.tags)
            if art.suspect_id == "queen":
                p_queen += art.points
            elif art.suspect_id == "chef":
                p_chef += art.points
            elif art.suspect_id == "goblin":
                p_goblin += art.points
        snap_surf = self.snapshot_freeze_surface.copy()
        self.snapshots.append(
            Snapshot(surface=snap_surf, tags=captured_tags, scene_label=scene.label, points_queen=p_queen, points_chef=p_chef, points_goblin=p_goblin, trigger_artifact_filename=None)
        )
        self.state = "snapshot_effect"

    def _update_snapshot_effect(self, dt: float) -> None:
        self.snapshot_effect_time += dt
        self.snapshot_flash_alpha = max(0, 255 - int(self.snapshot_effect_time * 800))
        if self.snapshot_effect_time > 0.4:
            self.state = "menu"
            self.current_scene_index = -1
            self.snapshot_freeze_surface = None

    def _handle_accuse_click(self, pos: Tuple[int, int]) -> None:
        card_w, card_h = 260, 115
        margin_x, margin_y = 60, 160
        spacing_y = 125
        for idx, s in enumerate(self.suspects):
            x, y = margin_x, margin_y + idx * spacing_y
            if x <= pos[0] <= x + card_w and y <= pos[1] <= y + card_h:
                self.selected_suspect = s
                self._compute_result()
                self.state = "result"
                self.result_time = 0.0
                break

    def _update_accuse_hover(self, mx: int, my: int) -> None:
        card_w, card_h = 260, 115
        margin_x, margin_y = 60, 160
        spacing_y = 125
        self.hover_suspect_index = -1
        for idx in range(len(self.suspects)):
            x, y = margin_x, margin_y + idx * spacing_y
            if x <= mx <= x + card_w and y <= my <= y + card_h:
                self.hover_suspect_index = idx
                break

    def _compute_result(self) -> None:
        if not self.selected_suspect:
            return
        total_queen = sum(s.points_queen for s in self.snapshots)
        total_chef = sum(s.points_chef for s in self.snapshots)
        total_goblin = sum(s.points_goblin for s in self.snapshots)
        sid = self.selected_suspect.id
        total_accused = total_queen if sid == "queen" else (total_chef if sid == "chef" else total_goblin)
        is_culprit = self.selected_suspect.id == self.culprit.id
        success = is_culprit and total_accused >= EVIDENCE_POINTS_REQUIRED
        self.result_success = success
        if success:
            self.result_message = (
                f"You accused {self.selected_suspect.name} and succeeded.\n"
                f"You had {total_accused} points of evidence against {self.selected_suspect.role}.\n"
                f"The culprit was indeed {self.culprit.name}."
            )
        else:
            if is_culprit:
                self.result_message = (
                    f"You accused {self.selected_suspect.name}, the real culprit, but you did not "
                    f"crystallize enough evidence. You had {total_accused} points (need {EVIDENCE_POINTS_REQUIRED})."
                )
            else:
                self.result_message = (
                    f"Wrong suspect. You accused {self.selected_suspect.name} but the real culprit was "
                    f"{self.culprit.name}."
                )

    # ---------- Draw: Opening sequence ----------
    def _wrap_opening_text(self, text: str, font: pygame.font.Font, max_width: int) -> List[str]:
        """Split text by newlines, word-wrap each paragraph to max_width. Returns list of lines."""
        lines_out: List[str] = []
        for para in text.split("\n"):
            words = para.split()
            line = []
            for word in words:
                test = " ".join(line + [word])
                if font.size(test)[0] <= max_width:
                    line.append(word)
                else:
                    if line:
                        lines_out.append(" ".join(line))
                    line = [word]
            if line:
                lines_out.append(" ".join(line))
        return lines_out

    def _draw_opening_text_box(self) -> None:
        """Draw the current opening slide text box at bottom with typing effect. Format: 'Speaker\\nDialogue'."""
        slide_idx = self.opening_slide_index
        if slide_idx >= len(OPENING_SCRIPT) or self.opening_text_index >= len(OPENING_SCRIPT[slide_idx]):
            return
        full_text = OPENING_SCRIPT[slide_idx][self.opening_text_index]
        display_text = full_text[: self.opening_char_index]
        font = self.popup_text_font
        speaker_font = self.popup_small_font
        box_margin_x = 80
        box_margin_bottom = 52
        box_max_width = self.WIDTH - 2 * box_margin_x
        line_height = font.get_height() + 4
        padding = 20
        # Split into speaker (first line) and dialogue (rest)
        if "\n" in display_text:
            speaker_text, dialogue_text = display_text.split("\n", 1)
        else:
            speaker_text = display_text if display_text else ""
            dialogue_text = ""
        wrapped = self._wrap_opening_text(dialogue_text, font, box_max_width - 48) if dialogue_text else []
        speaker_height = (speaker_font.get_height() + 2) if speaker_text else 0
        if speaker_text:
            speaker_height += 4  # gap below speaker
        box_h = speaker_height + (len(wrapped) * line_height + 2 * padding) if wrapped else (line_height + 2 * padding)
        box_h = min(max(box_h, 80), 200)
        box_y = self.HEIGHT - box_h - box_margin_bottom
        box_x = (self.WIDTH - box_max_width) // 2
        box_rect = pygame.Rect(box_x, box_y, box_max_width, box_h)
        # Semi-transparent dark panel with border
        panel = pygame.Surface((box_rect.w, box_rect.h), pygame.SRCALPHA)
        panel.fill((18, 16, 22, 220))
        pygame.draw.rect(panel, (80, 70, 55, 180), (0, 0, box_rect.w, box_rect.h), 2, border_radius=8)
        pygame.draw.rect(panel, (120, 105, 75, 120), (0, 0, box_rect.w, box_rect.h), 1, border_radius=8)
        self.screen.blit(panel, box_rect.topleft)
        text_color = (232, 225, 210)
        shadow_color = (40, 35, 30)
        speaker_color = (180, 168, 145)
        y = box_y + padding
        if speaker_text:
            s_shadow = speaker_font.render(speaker_text, True, shadow_color)
            s_surf = speaker_font.render(speaker_text, True, speaker_color)
            self.screen.blit(s_shadow, (box_x + padding + 1, y + 1))
            self.screen.blit(s_surf, (box_x + padding, y))
            y += speaker_font.get_height() + 6
        for i, line in enumerate(wrapped):
            shadow = font.render(line, True, shadow_color)
            surf = font.render(line, True, text_color)
            self.screen.blit(shadow, (box_x + padding + 1, y + 1))
            self.screen.blit(surf, (box_x + padding, y))
            y += line_height

    def draw_opening(self) -> None:
        self.screen.fill((0, 0, 0))
        if not self.opening_images or self.opening_slide_index >= len(self.opening_images):
            return
        img = self.opening_images[self.opening_slide_index]
        if self.opening_phase == "fade_in":
            alpha = min(255, int(255 * self.opening_timer / self.OPENING_FADE_IN_DURATION))
        elif self.opening_phase == "holding":
            alpha = 255
        else:  # fade_out
            alpha = max(0, int(255 * (1.0 - self.opening_timer / self.OPENING_FADE_OUT_DURATION)))
        img.set_alpha(alpha)
        self.screen.blit(img, (0, 0))
        if self.opening_phase == "holding":
            self._draw_opening_text_box()
            hint = self.small_font.render("RIGHT ARROW to continue  ·  ESC to skip", True, (140, 145, 155))
            self.screen.blit(hint, (self.WIDTH // 2 - hint.get_width() // 2, self.HEIGHT - hint.get_height() - 16))

    # ---------- Draw: Ending sequence ----------
    def _ending_memory_rects(self) -> List[Tuple[pygame.Rect, int]]:
        """Return [(rect, snapshot_index), ...] for the memories row (one per crystallized snapshot)."""
        if not self.snapshots:
            return []
        rects = []
        thumb_w, thumb_h = 100, 75
        row_y = self.HEIGHT - thumb_h - 50
        total_w = len(self.snapshots) * (thumb_w + 24) - 24
        start_x = (self.WIDTH - total_w) // 2
        for i in range(len(self.snapshots)):
            px = start_x + i * (thumb_w + 24)
            rect = pygame.Rect(px, row_y, thumb_w + 20, thumb_h + 24)
            rects.append((rect, i))
        return rects

    def _load_ending_popup_artifact(self, snapshot_index: int) -> None:
        """Load popup image for the snapshot's trigger artifact (for ending memory popup)."""
        self.popup_artifact_surface = None
        if snapshot_index < 0 or snapshot_index >= len(self.snapshots):
            return
        snap = self.snapshots[snapshot_index]
        fn = (snap.trigger_artifact_filename or "").strip()
        if not fn:
            return
        root = os.path.dirname(os.path.abspath(__file__))
        fn_lower = fn.lower()
        if fn_lower.startswith("r") and fn_lower.endswith(".png") and fn_lower[1:-4].isdigit():
            path = os.path.join(root, "replacements", fn)
        else:
            scene_index = next((i for i, sc in enumerate(self.scenes) if sc.label == snap.scene_label), 0)
            subfolder = f"s{scene_index + 1}"
            path = os.path.join(root, "scenes", subfolder, fn)
        try:
            img = pygame.image.load(path).convert_alpha()
            max_side = 200
            w, h = img.get_width(), img.get_height()
            if w > h:
                w, h = (max_side, max(1, int(h * max_side / w))) if w > max_side else (w, h)
            else:
                w, h = (max(1, int(w * max_side / h)), max_side) if h > max_side else (w, h)
            self.popup_artifact_surface = pygame.transform.smoothscale(img, (w, h))
        except (pygame.error, FileNotFoundError):
            pass

    def _handle_ending_memory_click(self, pos: Tuple[int, int]) -> None:
        """If click is on a memory thumbnail, open that snapshot's artifact popup."""
        for rect, idx in self._ending_memory_rects():
            if rect.collidepoint(pos):
                self.ending_memory_popup_index = idx
                self.popup_artifact_filename = self.snapshots[idx].trigger_artifact_filename or ""
                self._load_ending_popup_artifact(idx)
                return

    def _close_ending_memory_popup(self) -> None:
        self.ending_memory_popup_index = -1
        self.popup_artifact_filename = ""
        self.popup_artifact_surface = None

    def _draw_ending_artifact_popup(self) -> None:
        """Draw medieval-style artifact popup (image, name, description, X only) for ending memory."""
        if self.ending_memory_popup_index < 0 or not self.popup_artifact_filename:
            return
        info = ARTIFACT_INFO.get(self.popup_artifact_filename)
        if not info:
            return
        box_w, box_h = 520, 380
        box_x = (self.WIDTH - box_w) // 2
        box_y = (self.HEIGHT - box_h) // 2
        margin = 24
        pygame.draw.rect(self.screen, (48, 42, 35), (box_x, box_y, box_w, box_h), border_radius=8)
        pygame.draw.rect(self.screen, (95, 75, 52), (box_x, box_y, box_w, box_h), 3, border_radius=8)
        x_btn = pygame.Rect(box_x + box_w - 36, box_y + 8, 28, 28)
        pygame.draw.rect(self.screen, (68, 52, 38), x_btn)
        pygame.draw.line(self.screen, (180, 160, 120), (x_btn.left + 7, x_btn.top + 7), (x_btn.right - 7, x_btn.bottom - 7), 2)
        pygame.draw.line(self.screen, (180, 160, 120), (x_btn.right - 7, x_btn.top + 7), (x_btn.left + 7, x_btn.bottom - 7), 2)
        title_surf = self.popup_title_font.render(info["name"], True, (228, 212, 180))
        self.screen.blit(title_surf, (box_x + (box_w - title_surf.get_width()) // 2, box_y + 18))
        img_area_w, img_area_h = 200, 220
        img_area_x, img_area_y = box_x + margin + 12, box_y + 54
        if self.popup_artifact_surface is not None:
            surf = self.popup_artifact_surface
            sw, sh = surf.get_width(), surf.get_height()
            scale = min(img_area_w / max(1, sw), img_area_h / max(1, sh), 1.0)
            dw, dh = int(sw * scale), int(sh * scale)
            dx = img_area_x + (img_area_w - dw) // 2
            dy = img_area_y + (img_area_h - dh) // 2
            if scale < 1.0:
                surf = pygame.transform.smoothscale(surf, (dw, dh))
            self.screen.blit(surf, (dx, dy))
        desc_x = box_x + margin + 12 + img_area_w + 16
        desc = info["description"]
        max_line_w = box_w - (desc_x - box_x) - 24
        words, lines, line = desc.split(), [], []
        for word in words:
            test = " ".join(line + [word])
            if self.popup_text_font.size(test)[0] <= max_line_w:
                line.append(word)
            else:
                if line:
                    lines.append(" ".join(line))
                line = [word]
        if line:
            lines.append(" ".join(line))
        y_desc = box_y + 78
        lh = self.popup_text_font.get_height() + 3
        for i, ln in enumerate(lines):
            if y_desc + (i + 1) * lh > box_y + box_h - 50:
                break
            self.screen.blit(self.popup_text_font.render(ln, True, (210, 195, 165)), (desc_x, y_desc + i * lh))
        close_hint = self.popup_small_font.render("Close (X)", True, (165, 145, 110))
        self.screen.blit(close_hint, (box_x + box_w - 24 - close_hint.get_width(), box_y + box_h - 36))

    def _draw_ending_text_box(self) -> None:
        """Draw ending slide text box (same style as opening). Uses slide's script, ending_text_index, ending_char_index."""
        if self.ending_index >= len(self.ending_slides):
            return
        slide = self.ending_slides[self.ending_index]
        script = slide.get("script", [])
        if not script or self.ending_text_index >= len(script):
            return
        full_text = script[self.ending_text_index]
        display_text = full_text[: self.ending_char_index]
        font = self.popup_text_font
        speaker_font = self.popup_small_font
        box_margin_x = 80
        box_margin_bottom = 52
        box_max_width = self.WIDTH - 2 * box_margin_x
        line_height = font.get_height() + 4
        padding = 20
        if "\n" in display_text:
            speaker_text, dialogue_text = display_text.split("\n", 1)
        else:
            speaker_text = display_text if display_text else ""
            dialogue_text = ""
        wrapped = self._wrap_opening_text(dialogue_text, font, box_max_width - 48) if dialogue_text else []
        speaker_height = (speaker_font.get_height() + 2) if speaker_text else 0
        if speaker_text:
            speaker_height += 4
        box_h = speaker_height + (len(wrapped) * line_height + 2 * padding) if wrapped else (line_height + 2 * padding)
        box_h = min(max(box_h, 80), 220)
        box_margin_top = 52
        if slide.get("show_memories"):
            box_y = box_margin_top
        else:
            box_y = self.HEIGHT - box_h - box_margin_bottom
        box_x = (self.WIDTH - box_max_width) // 2
        box_rect = pygame.Rect(box_x, box_y, box_max_width, box_h)
        panel = pygame.Surface((box_rect.w, box_rect.h), pygame.SRCALPHA)
        panel.fill((18, 16, 22, 220))
        pygame.draw.rect(panel, (80, 70, 55, 180), (0, 0, box_rect.w, box_rect.h), 2, border_radius=8)
        pygame.draw.rect(panel, (120, 105, 75, 120), (0, 0, box_rect.w, box_rect.h), 1, border_radius=8)
        self.screen.blit(panel, box_rect.topleft)
        text_color = (232, 225, 210)
        shadow_color = (40, 35, 30)
        speaker_color = (180, 168, 145)
        y = box_y + padding
        if speaker_text:
            s_shadow = speaker_font.render(speaker_text, True, shadow_color)
            s_surf = speaker_font.render(speaker_text, True, speaker_color)
            self.screen.blit(s_shadow, (box_x + padding + 1, y + 1))
            self.screen.blit(s_surf, (box_x + padding, y))
            y += speaker_font.get_height() + 6
        for i, line in enumerate(wrapped):
            shadow = font.render(line, True, shadow_color)
            surf = font.render(line, True, text_color)
            self.screen.blit(shadow, (box_x + padding + 1, y + 1))
            self.screen.blit(surf, (box_x + padding, y))
            y += line_height

    def draw_ending(self) -> None:
        self.screen.fill((0, 0, 0))
        if self.ending_index >= len(self.ending_slides):
            return
        slide = self.ending_slides[self.ending_index]
        script = slide.get("script", [])
        img_name = slide.get("image")
        if img_name:
            surf = self._load_ending_image(img_name)
            if self.ending_phase == "fade_in":
                alpha = min(255, int(255 * self.ending_timer / self.ENDING_FADE_IN))
            elif self.ending_phase == "holding":
                alpha = 255
            else:
                alpha = max(0, int(255 * (1.0 - self.ending_timer / self.ENDING_FADE_OUT)))
            surf.set_alpha(alpha)
            self.screen.blit(surf, (0, 0))
        show_memories = slide.get("show_memories") and len(self.snapshots) > 0
        if show_memories:
            thumb_w, thumb_h = 100, 75
            row_y = self.HEIGHT - thumb_h - 50
            total_w = len(self.snapshots) * (thumb_w + 24) - 24
            start_x = (self.WIDTH - total_w) // 2
            for i, snap in enumerate(self.snapshots):
                px = start_x + i * (thumb_w + 24)
                rect = pygame.Rect(px, row_y, thumb_w + 20, thumb_h + 24)
                tilt = (-5 + (i % 3) * 5) * (math.pi / 180)
                draw_polaroid_frame(
                    self.screen, rect,
                    pygame.transform.smoothscale(snap.surface, (thumb_w, thumb_h)),
                    tilt=tilt * 10,
                )
        if script and self.ending_text_index < len(script):
            self._draw_ending_text_box()
            if not slide.get("accept_123"):
                hint = self.small_font.render("RIGHT ARROW to continue", True, (140, 145, 155))
                self.screen.blit(hint, (self.WIDTH // 2 - hint.get_width() // 2, self.HEIGHT - 28))
            else:
                choose_hint = self.small_font.render("Press 1 (Goblin), 2 (Chef), or 3 (Queen) to choose", True, (140, 145, 155))
                self.screen.blit(choose_hint, (self.WIDTH // 2 - choose_hint.get_width() // 2, self.HEIGHT - 28))
        elif slide.get("exit_prompt") and (not script or self.ending_text_index >= len(script)):
            hint = self.text_font.render("Click or press a key to exit", True, (200, 205, 220))
            self.screen.blit(hint, (self.WIDTH // 2 - hint.get_width() // 2, self.HEIGHT - 42))
            # Music credit (Yakov Golman, Free Music Archive, CC BY)
            credit_font = pygame.font.SysFont("arial", 11)
            credit = credit_font.render("Music: Yakov Golman (Free Music Archive, CC BY)", True, (100, 105, 110))
            self.screen.blit(credit, (self.WIDTH // 2 - credit.get_width() // 2, self.HEIGHT - 20))
        elif self.ending_phase == "holding" and not slide.get("accept_123") and not script:
            hint = self.small_font.render("RIGHT ARROW to continue", True, (140, 145, 155))
            self.screen.blit(hint, (self.WIDTH // 2 - hint.get_width() // 2, self.HEIGHT - 28))
        elif slide.get("accept_123"):
            hint = self.small_font.render("Press 1 (Goblin), 2 (Chef), or 3 (Queen) to choose", True, (140, 145, 155))
            self.screen.blit(hint, (self.WIDTH // 2 - hint.get_width() // 2, self.HEIGHT - 28))
        if self.ending_memory_popup_index >= 0:
            self._draw_ending_artifact_popup()

    # ---------- Draw: Menu (clock selection) ----------
    # (draw_menu dispatcher and _draw_menu_impl are below draw_result)

    # ---------- Draw: Memory scene ----------
    def draw_scene(self) -> None:
        if self.current_scene_index < 0:
            return
        scene = self.scenes[self.current_scene_index]
        t = self.scene_time
        px, py = int(self.parallax_offset[0]), int(self.parallax_offset[1])
        shake_x, shake_y = int(self.camera_shake[0]), int(self.camera_shake[1])

        # Background (s1–s6 image or gradient fallback)
        bg = scene.background
        fade = self.scene_fade_progress[self.current_scene_index] if 0 <= self.current_scene_index < len(self.scene_fade_progress) else 0.0
        if fade > 0.02:
            # Fade + blur: scale down then up for soft blur, then dull overlay
            scale = 1.0 - 0.45 * fade
            if scale < 0.55:
                scale = 0.55
            bw, bh = bg.get_width(), bg.get_height()
            small_w = max(1, int(bw * scale))
            small_h = max(1, int(bh * scale))
            blurred = pygame.transform.smoothscale(bg, (small_w, small_h))
            blurred = pygame.transform.smoothscale(blurred, (bw, bh))
            self.screen.blit(blurred, (0, 0))
        else:
            self.screen.blit(bg, (0, 0))
        # Artifacts: draw before dull overlay so they fade at same rate as background; track rects for hover
        ox, oy, bw, bh = scene.bg_rect
        artifact_rects = []
        for art in scene.artifacts:
            cx = ox + int(art.frac_x * bw)
            cy = oy + int(art.frac_y * bh)
            sw, sh = art.surface.get_width(), art.surface.get_height()
            dx = cx - sw // 2 + shake_x + int(art.offset_x_aw * sw)
            dy = cy - sh // 2 + shake_y + int(art.offset_y_ah * sh)
            artifact_rects.append(pygame.Rect(dx, dy, sw, sh))
            if art.darken < 1.0:
                temp = pygame.Surface((sw, sh), pygame.SRCALPHA)
                temp.blit(art.surface, (0, 0))
                dark = pygame.Surface((sw, sh))
                v = int(255 * art.darken)
                dark.fill((v, v, v))
                temp.blit(dark, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
                self.screen.blit(temp, (dx, dy))
            else:
                self.screen.blit(art.surface, (dx, dy))
        # Hover highlight: lighten hovered artifact (same style as clock menu)
        mx, my = pygame.mouse.get_pos()
        hovered_idx = next((i for i, r in enumerate(artifact_rects) if r.collidepoint(mx, my)), -1)
        if hovered_idx >= 0:
            art = scene.artifacts[hovered_idx]
            r = artifact_rects[hovered_idx]
            lighten = art.surface.copy()
            lighten.set_alpha(70)
            self.screen.blit(lighten, r.topleft, special_flags=pygame.BLEND_RGBA_ADD)
        # Cursor: pointer when hovering over an artifact (clickable), arrow otherwise
        try:
            if hovered_idx >= 0:
                pygame.mouse.set_cursor(pygame.cursors.Cursor(pygame.SYSTEM_CURSOR_HAND))
            else:
                pygame.mouse.set_cursor(pygame.cursors.Cursor(pygame.SYSTEM_CURSOR_ARROW))
        except (AttributeError, TypeError):
            pass
        # Dull overlay: fades/dulls the scene (no blackening), same rate for bg and artifacts
        if fade > 0:
            dull = pygame.Surface((self.WIDTH, self.SCENE_HEIGHT), pygame.SRCALPHA)
            alpha = int(fade * 248)
            dull.fill((238, 240, 245))
            dull.set_alpha(min(255, alpha))
            self.screen.blit(dull, (0, 0))
        # Light vignette
        draw_vignette_fast(self.screen.subsurface((0, 0, self.WIDTH, self.SCENE_HEIGHT)), 0.15)

        # Slim bottom bar: one line
        panel_y = self.SCENE_HEIGHT
        panel_h = self.HEIGHT - panel_y
        pygame.draw.rect(self.screen, (22, 28, 42), (0, panel_y, self.WIDTH, panel_h))
        pygame.draw.line(self.screen, (50, 60, 90), (0, panel_y), (self.WIDTH, panel_y), 1)
        hint = self.small_font.render(
            f"  {scene.label}  ·  S: snapshot ({len(self.snapshots)}/{self.MAX_SNAPSHOTS})  ·  ESC: back to clocks",
            True, (180, 190, 210)
        )
        self.screen.blit(hint, (self.WIDTH // 2 - hint.get_width() // 2, panel_y + (panel_h - hint.get_height()) // 2))

    # ---------- Artifact popup (medieval textbox) ----------
    def _draw_artifact_popup(self) -> None:
        info = ARTIFACT_INFO.get(self.popup_artifact_filename)
        if not info:
            return
        box_w, box_h = 520, 440
        box_x = (self.WIDTH - box_w) // 2
        box_y = (self.SCENE_HEIGHT - box_h) // 2
        # Dark overlay
        overlay = pygame.Surface((self.WIDTH, self.SCENE_HEIGHT), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 170))
        self.screen.blit(overlay, (0, 0))
        # Medieval frame: outer shadow/dark band
        margin = 12
        pygame.draw.rect(self.screen, (35, 28, 22), (box_x - 2, box_y - 2, box_w + 4, box_h + 4))
        # Outer border (dark wood / iron)
        pygame.draw.rect(self.screen, (55, 42, 32), (box_x, box_y, box_w, box_h), 4)
        # Inner margin band (lighter)
        pygame.draw.rect(self.screen, (75, 58, 42), (box_x + 4, box_y + 4, box_w - 8, box_h - 8), 2)
        # Parchment fill with gradient (darker edges)
        inner_h = box_h - 2 * margin
        for dy in range(inner_h):
            t = dy / max(1, inner_h)
            edge = min(dy, inner_h - 1 - dy, margin * 2) / (margin * 2) if margin else 1.0
            r = int(72 + 18 * (1 - t) + 12 * (1 - edge))
            g = int(58 + 14 * (1 - t) + 10 * (1 - edge))
            b = int(42 + 10 * (1 - t) + 8 * (1 - edge))
            r, g, b = max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b))
            pygame.draw.line(self.screen, (r, g, b), (box_x + margin, box_y + margin + dy), (box_x + box_w - margin - 1, box_y + margin + dy))
        # Ornate corner flourishes (L-shaped)
        flourish_w = 20
        fc = (100, 78, 55)
        for (cx, cy), (dx, dy) in [((box_x, box_y), (1, 1)), ((box_x + box_w, box_y), (-1, 1)), ((box_x + box_w, box_y + box_h), (-1, -1)), ((box_x, box_y + box_h), (1, -1))]:
            pygame.draw.line(self.screen, fc, (cx, cy + 8), (cx, cy + flourish_w * dy), 2)
            pygame.draw.line(self.screen, fc, (cx + 8 * dx, cy), (cx + flourish_w * dx, cy), 2)
            pygame.draw.line(self.screen, (130, 100, 72), (cx + 2 * dx, cy + 2 * dy), (cx + 6 * dx, cy + 6 * dy), 1)
        # Top/bottom decorative double line
        pygame.draw.line(self.screen, (90, 70, 50), (box_x + 28, box_y + 44), (box_x + box_w - 28, box_y + 44), 1)
        pygame.draw.line(self.screen, (110, 85, 60), (box_x + 28, box_y + 46), (box_x + box_w - 28, box_y + 46), 1)
        # X close button (engraved look)
        x_btn = pygame.Rect(box_x + box_w - 36, box_y + 8, 28, 28)
        pygame.draw.rect(self.screen, (58, 45, 35), x_btn)
        pygame.draw.rect(self.screen, (95, 75, 52), x_btn, 2)
        pygame.draw.line(self.screen, (180, 160, 120), (x_btn.left + 7, x_btn.top + 7), (x_btn.right - 7, x_btn.bottom - 7), 2)
        pygame.draw.line(self.screen, (180, 160, 120), (x_btn.right - 7, x_btn.top + 7), (x_btn.left + 7, x_btn.bottom - 7), 2)
        # Title (artifact name) — serif, slight shadow
        title_surf = self.popup_title_font.render(info["name"], True, (45, 38, 28))
        self.screen.blit(title_surf, (box_x + (box_w - title_surf.get_width()) // 2 + 1, box_y + 18 + 1))
        title_surf = self.popup_title_font.render(info["name"], True, (228, 212, 180))
        self.screen.blit(title_surf, (box_x + (box_w - title_surf.get_width()) // 2, box_y + 18))
        # Artifact image (larger version) on the left
        img_area_w, img_area_h = 200, 220
        img_area_x = box_x + margin + 12
        img_area_y = box_y + 54
        if self.popup_artifact_surface is not None:
            # Frame for the image (engraved look)
            img_frame = pygame.Rect(img_area_x, img_area_y, img_area_w, img_area_h)
            pygame.draw.rect(self.screen, (48, 38, 28), img_frame)
            pygame.draw.rect(self.screen, (85, 68, 48), img_frame, 2)
            pygame.draw.line(self.screen, (65, 52, 38), img_frame.topleft, img_frame.bottomleft, 1)
            pygame.draw.line(self.screen, (65, 52, 38), img_frame.topleft, img_frame.topright, 1)
            # Center image in frame (scale to fit if needed)
            surf = self.popup_artifact_surface
            sw, sh = surf.get_width(), surf.get_height()
            scale = min(img_area_w / max(1, sw), img_area_h / max(1, sh), 1.0)
            dw, dh = int(sw * scale), int(sh * scale)
            dx = img_area_x + (img_area_w - dw) // 2
            dy = img_area_y + (img_area_h - dh) // 2
            if scale < 1.0:
                scaled = pygame.transform.smoothscale(surf, (dw, dh))
                self.screen.blit(scaled, (dx, dy))
            else:
                self.screen.blit(surf, (dx, dy))
        # "Description" label and text to the right of the image
        desc_x = box_x + margin + 12 + img_area_w + 16
        desc_label = self.popup_small_font.render("Description", True, (160, 140, 105))
        self.screen.blit(desc_label, (desc_x, box_y + 54))
        pygame.draw.line(self.screen, (100, 82, 58), (desc_x, box_y + 54 + desc_label.get_height() + 2), (desc_x + desc_label.get_width(), box_y + 54 + desc_label.get_height() + 2), 1)
        # Description text (wrapped, serif)
        desc = info["description"]
        max_line_w = box_w - (desc_x - box_x) - 24
        words = desc.split()
        lines = []
        line = []
        for word in words:
            test = " ".join(line + [word])
            if self.popup_text_font.size(test)[0] <= max_line_w:
                line.append(word)
            else:
                if line:
                    lines.append(" ".join(line))
                line = [word]
        if line:
            lines.append(" ".join(line))
        y_desc = box_y + 78
        line_height = self.popup_text_font.get_height() + 3
        for i, ln in enumerate(lines):
            if y_desc + (i + 1) * line_height > box_y + box_h - 118:
                break
            shadow = self.popup_text_font.render(ln, True, (50, 42, 32))
            self.screen.blit(shadow, (desc_x + 1, y_desc + i * line_height + 1))
            surf = self.popup_text_font.render(ln, True, (210, 195, 165))
            self.screen.blit(surf, (desc_x, y_desc + i * line_height))
        # Crystallizations (serif, ornamental)
        used = len(self.snapshots)
        remain = self.MAX_SNAPSHOTS - used
        cryst_text = self.popup_small_font.render(
            f"  {used} of {self.MAX_SNAPSHOTS} crystallizations used   ·   {remain} remaining  ",
            True, (165, 145, 110)
        )
        self.screen.blit(cryst_text, (box_x + (box_w - cryst_text.get_width()) // 2, box_y + box_h - 98))
        # Buttons: Crystallize when < 3 (can use multiple from same scene); Uncrystallize only from the artifact that triggered it
        btn_y = box_y + box_h - 56
        btn_h = 40
        scene = self.scenes[self.popup_scene_index] if self.popup_scene_index >= 0 else None
        can_uncrystallize_here = bool(
            scene and any(
                s.scene_label == scene.label and (s.trigger_artifact_filename is None or s.trigger_artifact_filename == self.popup_artifact_filename)
                for s in self.snapshots
            )
        )
        cryst_btn = pygame.Rect(box_x + 24, btn_y, 160, btn_h)
        uncryst_btn = pygame.Rect(box_x + 24 + 164, btn_y, 130, btn_h)
        close_btn = pygame.Rect(box_x + box_w - 24 - 100, btn_y, 100, btn_h)
        buttons = []
        if len(self.snapshots) < self.MAX_SNAPSHOTS:
            buttons.append((cryst_btn, "Crystallize memory"))
        else:
            buttons.append((cryst_btn, "Crystallize memory (full)"))
        if can_uncrystallize_here:
            buttons.append((uncryst_btn, "Uncrystallize"))
        buttons.append((close_btn, "Close"))
        for btn_rect, label in buttons:
            pygame.draw.rect(self.screen, (68, 52, 38), btn_rect)
            pygame.draw.line(self.screen, (115, 90, 62), btn_rect.topleft, btn_rect.topright, 2)
            pygame.draw.line(self.screen, (115, 90, 62), btn_rect.topleft, btn_rect.bottomleft, 2)
            pygame.draw.line(self.screen, (45, 35, 25), btn_rect.bottomleft, btn_rect.bottomright, 2)
            pygame.draw.line(self.screen, (45, 35, 25), btn_rect.topright, btn_rect.bottomright, 2)
            pygame.draw.rect(self.screen, (88, 68, 48), btn_rect.inflate(-4, -4), 1)
            color = (180, 170, 150) if label == "Crystallize memory (full)" else (225, 210, 178)
            lbl_surf = self.popup_small_font.render(label, True, color)
            self.screen.blit(lbl_surf, (btn_rect.centerx - lbl_surf.get_width() // 2, btn_rect.centery - lbl_surf.get_height() // 2))

    # ---------- Snapshot effect (freeze, flash, desaturate) ----------
    def _draw_snapshot_effect(self) -> None:
        if self.snapshot_freeze_surface is None:
            return
        self.screen.blit(self.snapshot_freeze_surface, (0, 0))
        # Desaturate briefly
        desat = pygame.Surface(self.snapshot_freeze_surface.get_size(), pygame.SRCALPHA)
        desat.fill((180, 180, 180, 60))
        self.screen.blit(desat, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        # White flash
        if self.snapshot_flash_alpha > 0:
            flash = pygame.Surface((self.WIDTH, self.SCENE_HEIGHT), pygame.SRCALPHA)
            flash.fill((255, 255, 255, self.snapshot_flash_alpha))
            self.screen.blit(flash, (0, 0))

    # ---------- Draw: Accusation ----------
    def draw_accuse(self) -> None:
        t = self._time_accum
        for y in range(self.HEIGHT):
            v = y / self.HEIGHT
            r, g, b = int(8 + 12 * (1 - v)), int(12 + 18 * (1 - v)), int(28 + 25 * (1 - v))
            pygame.draw.line(self.screen, (r, g, b), (0, y), (self.WIDTH, y))
        draw_vignette_fast(self.screen, 0.5)
        draw_glitch_overlay(self.screen, 0.1, t)

        title = self.title_font.render("Make Your Accusation", True, (230, 235, 245))
        self.screen.blit(title, (self.WIDTH // 2 - title.get_width() // 2, 22))
        inst = self.text_font.render("Crystallize at least 12 points of evidence for your chosen suspect. Click to accuse.", True, (190, 198, 210))
        self.screen.blit(inst, (self.WIDTH // 2 - inst.get_width() // 2, 68))

        total_queen = sum(s.points_queen for s in self.snapshots)
        total_chef = sum(s.points_chef for s in self.snapshots)
        total_goblin = sum(s.points_goblin for s in self.snapshots)
        pts_line = self.small_font.render(f"Evidence: Queen {total_queen} pts  ·  Chef {total_chef} pts  ·  Goblin {total_goblin} pts", True, (170, 178, 195))
        self.screen.blit(pts_line, (self.WIDTH // 2 - pts_line.get_width() // 2, 96))

        card_w, card_h = 260, 115
        margin_x, margin_y = 60, 180
        spacing_y = 125
        for idx, s in enumerate(self.suspects):
            x, y = margin_x, margin_y + idx * spacing_y
            hover = self.hover_suspect_index == idx
            # Dossier card with paper texture (noise) and lift
            lift = ease_out_quad(min(1.0, 0.3 + 0.15 * math.sin(t + idx))) if hover else 0
            draw_y = int(y - lift * 6)
            rect = pygame.Rect(x, draw_y, card_w, card_h)
            # Shadow
            shadow_r = pygame.Rect(x + 5, draw_y + 5, card_w, card_h)
            shadow = pygame.Surface((card_w + 10, card_h + 10), pygame.SRCALPHA)
            pygame.draw.rect(shadow, (0, 0, 0, 70), (5, 5, card_w, card_h), border_radius=6)
            self.screen.blit(shadow, (x - 2, draw_y - 2))
            # Paper colour with slight variation (dossier)
            paper = (38, 42, 58) if not hover else (45, 50, 68)
            pygame.draw.rect(self.screen, paper, rect, border_radius=6)
            # Red outline when selected (we don't have selection until click, so use hover)
            border_color = (180, 80, 80) if hover else (70, 95, 140)
            border_w = 3 if hover else 1
            pygame.draw.rect(self.screen, border_color, rect, border_w, border_radius=6)
            # Paper texture (light noise)
            for _ in range(80):
                nx = x + random.randint(0, card_w - 1)
                ny = draw_y + random.randint(0, card_h - 1)
                self.screen.set_at((nx, ny), (50, 55, 75))
            name = self.text_font.render(s.name, True, (235, 238, 248))
            role = self.small_font.render(s.role, True, (180, 188, 205))
            pts = total_queen if s.id == "queen" else (total_chef if s.id == "chef" else total_goblin)
            motive = self.small_font.render(f"Motive: {s.motive}", True, (170, 178, 195))
            pts_str = self.small_font.render(f"Your evidence: {pts} pts", True, (150, 200, 180) if pts >= EVIDENCE_POINTS_REQUIRED else (170, 178, 195))
            self.screen.blit(name, (x + 14, draw_y + 10))
            self.screen.blit(role, (x + 14, draw_y + 34))
            self.screen.blit(motive, (x + 14, draw_y + 56))
            self.screen.blit(pts_str, (x + 14, draw_y + 80))

        # Evidence board
        ref_x, ref_y = margin_x + card_w + 50, margin_y
        ref_w, ref_h = 380, 280
        board_rect = pygame.Rect(ref_x, ref_y, ref_w, ref_h)
        pygame.draw.rect(self.screen, (22, 28, 45), board_rect, border_radius=8)
        pygame.draw.rect(self.screen, (60, 85, 130), board_rect, 2, border_radius=8)
        ref_title = self.text_font.render("Evidence Board", True, (230, 235, 245))
        self.screen.blit(ref_title, (ref_x + 14, ref_y + 12))
        line_y = ref_y + 44
        for i, snap in enumerate(self.snapshots):
            tags_str = ", ".join(sorted(set(snap.tags)))
            line = self.small_font.render(f"{i + 1}. {snap.scene_label}: {tags_str}", True, (200, 208, 225))
            self.screen.blit(line, (ref_x + 14, line_y))
            line_y += 24

        # Snapshot polaroids on the right
        polaroid_y = ref_y + ref_h + 20
        thumb_w, thumb_h = 100, 75
        for i, snap in enumerate(self.snapshots):
            px = ref_x + i * (thumb_w + 30)
            tilt = (-5 + (i % 3) * 5) * (math.pi / 180)
            rect = pygame.Rect(px, polaroid_y, thumb_w + 20, thumb_h + 24)
            draw_polaroid_frame(
                self.screen, rect,
                pygame.transform.smoothscale(snap.surface, (thumb_w, thumb_h)),
                tilt=tilt * 10,
            )

    # ---------- Draw: Result ----------
    def draw_result(self) -> None:
        t = self.result_time
        if self.result_success:
            # Green pulse background
            pulse = 0.5 + 0.2 * math.sin(t * 2)
            base = (int(15 + 20 * pulse), int(35 + 25 * pulse), int(25 + 20 * pulse))
            self.screen.fill(base)
            draw_vignette_fast(self.screen, 0.4)
            big_text = "CASE CLOSED"
            alpha = min(255, int(200 + 55 * ease_out_quad(min(1.0, t * 1.5))))
            title_surf = self.big_result_font.render(big_text, True, (120, 255, 160))
            title_surf.set_alpha(alpha)
            self.screen.blit(
                title_surf,
                (self.WIDTH // 2 - title_surf.get_width() // 2, self.HEIGHT // 2 - 80),
            )
        else:
            # Red fade and glitch
            glitch = 0.3 * math.sin(t * 15) * (1 if t < 0.5 else 0.5)
            self.screen.fill((25, 8, 8))
            draw_glitch_overlay(self.screen, 0.4 + glitch, t)
            red_overlay = pygame.Surface((self.WIDTH, self.HEIGHT), pygame.SRCALPHA)
            red_overlay.fill((120, 0, 0, int(80 * (0.5 + 0.5 * math.sin(t * 2)))))
            self.screen.blit(red_overlay, (0, 0))
            draw_vignette_fast(self.screen, 0.6)
            big_text = "MEMORY COLLAPSED"
            title_surf = self.big_result_font.render(big_text, True, (220, 80, 80))
            self.screen.blit(
                title_surf,
                (self.WIDTH // 2 - title_surf.get_width() // 2, self.HEIGHT // 2 - 80),
            )

        if self.result_message:
            lines = self.result_message.split("\n")
            y = self.HEIGHT // 2 + 10
            for line in lines:
                rendered = self.text_font.render(line, True, (210, 215, 225))
                self.screen.blit(rendered, (60, y))
                y += 28

        # Polaroid thumbnails
        thumb_w, thumb_h = 160, 105
        for i, snap in enumerate(self.snapshots):
            px = 60 + i * (thumb_w + 40)
            py = self.HEIGHT - 140
            tilt = (-4 + i * 3) * (math.pi / 180)
            rect = pygame.Rect(px, py, thumb_w + 24, thumb_h + 28)
            draw_polaroid_frame(
                self.screen, rect,
                pygame.transform.smoothscale(snap.surface, (thumb_w, thumb_h)),
                tilt=tilt * 15,
            )
            lbl = self.small_font.render(snap.scene_label, True, (200, 205, 220))
            self.screen.blit(lbl, (px, py + thumb_h + 32))

        exit_msg = self.small_font.render("Click or press ESC to exit", True, (180, 185, 200))
        self.screen.blit(exit_msg, (60, self.HEIGHT - 28))

    # ---------- Main draw dispatcher (including snapshot effect state) ----------
    def draw_menu(self) -> None:
        if self.state == "snapshot_effect":
            self._draw_snapshot_effect()
            return
        self._draw_menu_impl()

    def _draw_menu_impl(self) -> None:
        """Actual menu draw (called when state is menu)."""
        t = self.menu_time
        mouse_x, mouse_y = pygame.mouse.get_pos()
        # Background: menu.png or fallback
        if self.menu_bg is not None:
            self.screen.blit(self.menu_bg, (0, 0))
        else:
            self.screen.fill((18, 22, 35))
            draw_vignette_fast(self.screen, 0.35)
        draw_glitch_overlay(self.screen, 0.06, t)
        # Clocks: c1–c6 images around center crystal (when assets loaded)
        hovered_clock_idx = -1
        if len(self.clock_images) >= 6 and len(self.clock_rects) >= 6 and self.clock_rects[0].width > 0:
            for idx in range(6):
                img = self.clock_images[idx]
                rect = self.clock_rects[idx]
                hovered = rect.collidepoint(mouse_x, mouse_y) and self.global_time > 0
                if hovered:
                    hovered_clock_idx = idx
                if img.get_width() > 1:
                    if self.global_time <= 0:
                        dimmed = img.copy()
                        dimmed.set_alpha(140)
                        self.screen.blit(dimmed, rect.topleft)
                    else:
                        self.screen.blit(img, rect.topleft)
                        if hovered:
                            lighten = img.copy()
                            lighten.set_alpha(70)
                            self.screen.blit(lighten, rect.topleft, special_flags=pygame.BLEND_RGBA_ADD)
            # Scene description when hovering a clock (centered above timer bar)
            if hovered_clock_idx >= 0 and hovered_clock_idx < len(self.clock_scene_descriptions):
                desc = self.clock_scene_descriptions[hovered_clock_idx]
                desc_surf = self.text_font.render(desc, True, (240, 242, 250))
                desc_rect = desc_surf.get_rect(center=(self.WIDTH // 2, 50))
                pad = 10
                bg_rect = desc_rect.inflate(pad * 2, pad)
                bg = pygame.Surface((bg_rect.width, bg_rect.height), pygame.SRCALPHA)
                bg.fill((0, 0, 0, 200))
                self.screen.blit(bg, bg_rect.topleft)
                pygame.draw.rect(self.screen, (90, 100, 130), bg_rect, 1, border_radius=6)
                self.screen.blit(desc_surf, desc_rect)
        else:
            # Fallback: procedural clock circles (only need fill if we didn't draw menu_bg)
            if self.menu_bg is None:
                self.screen.fill((18, 22, 35))
                draw_vignette_fast(self.screen, 0.35)
            for idx, scene in enumerate(self.scenes):
                cx, cy = self._clock_center(idx)
                hover = (mouse_x - cx) ** 2 + (mouse_y - cy) ** 2 <= self.CLOCK_RADIUS ** 2
                base_color = (60, 70, 90) if self.global_time <= 0 else ((100, 160, 220) if hover else (80, 130, 190))
                pulse = 0.8 + 0.2 * math.sin(t * 2 + idx * 0.5)
                draw_glowing_circle(self.screen, (cx, cy), self.CLOCK_RADIUS - 4, base_color, pulse)
                tick_angle = (t * 0.5 + idx) % (2 * math.pi)
                tx = cx + (self.CLOCK_RADIUS - 12) * math.cos(tick_angle)
                ty = cy + (self.CLOCK_RADIUS - 12) * math.sin(tick_angle)
                pygame.draw.line(self.screen, (200, 220, 255), (cx, cy), (int(tx), int(ty)), 2)
                lbl = self.text_font.render(scene.label, True, (25, 30, 45))
                self.screen.blit(lbl, (cx - lbl.get_width() // 2, cy - lbl.get_height() // 2))
        # Timer bar and text (on top of menu)
        bar_x, bar_y = 50, 72
        bar_w, bar_h = self.WIDTH - 100, 10
        overlay = pygame.Surface((bar_w + 20, 50), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 120))
        self.screen.blit(overlay, (bar_x - 10, bar_y - 8))
        pygame.draw.rect(self.screen, (30, 38, 55), (bar_x, bar_y, bar_w, bar_h), border_radius=4)
        if self.GLOBAL_TIME_LIMIT > 0:
            pct = self.global_time / self.GLOBAL_TIME_LIMIT
            pygame.draw.rect(self.screen, (70, 140, 200), (bar_x, bar_y, int(bar_w * pct), bar_h), border_radius=4)
        timer_text = self.small_font.render(f"{int(self.global_time)}s left  ·  Snapshots: {len(self.snapshots)}/{self.MAX_SNAPSHOTS}", True, (180, 190, 210))
        self.screen.blit(timer_text, (self.WIDTH // 2 - timer_text.get_width() // 2, bar_y + 14))
        if self.sounds and (self.heartbeat_channel is None or not self.heartbeat_channel.get_busy()):
            self.heartbeat_channel = self.sounds["heartbeat"].play(loops=0)
            if self.heartbeat_channel is not None:
                self.heartbeat_channel.set_volume(0.2)
        if self.global_time <= 0 and len(self.snapshots) < self.MAX_SNAPSHOTS:
            warn = self.small_font.render("Time's up. Proceed to accusation.", True, (220, 100, 100))
            self.screen.blit(warn, (self.WIDTH // 2 - warn.get_width() // 2, bar_y + 36))
        # Accuse button (bottom-right)
        accuse_rect = pygame.Rect(self.WIDTH - 200, self.HEIGHT - 56, 180, 42)
        accuse_hover = accuse_rect.collidepoint(mouse_x, mouse_y)
        btn_color = (90, 120, 170) if accuse_hover else (50, 70, 110)
        pygame.draw.rect(self.screen, btn_color, accuse_rect, border_radius=8)
        pygame.draw.rect(self.screen, (120, 150, 200), accuse_rect, 2, border_radius=8)
        acc_text = self.text_font.render("Accuse", True, (230, 235, 245))
        self.screen.blit(acc_text, (accuse_rect.centerx - acc_text.get_width() // 2, accuse_rect.centery - acc_text.get_height() // 2))


def main() -> None:
    pygame.init()
    screen = pygame.display.set_mode((VanishingMemoriesGame.WIDTH, VanishingMemoriesGame.HEIGHT))
    game = VanishingMemoriesGame(screen)
    game.run()


if __name__ == "__main__":
    main()
