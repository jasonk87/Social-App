#!/usr/bin/env python3
"""
social_app/server.py

This module implements a lightweight social media simulation server that runs
entirely on the local machine.  It uses only Python's standard library to
provide a simple HTTP API and a minimal web interface.  Users can create
communities (akin to subreddits), generate AI personas, and run
simulations that produce posts and comments using a locally running
Ollama instance.  All data is stored in a SQLite database located in the
project directory.  If Ollama is unavailable, the API calls will raise
exceptions so that the user knows something is wrong.  The simulation
engine runs in the background using separate threads.

To start the server run:
    python3 server.py

By default the server listens on localhost port 5000.  Open a browser
and navigate to http://localhost:5000/ to access the UI.
"""
from __future__ import annotations

import json
import os
import random
import secrets
import sqlite3
import threading
import time
import hashlib
import sys
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, unquote
from typing import Dict, List, Optional, Any

import requests
import queue
import heapq
import dataclasses
from enum import IntEnum
import re
from collections import Counter

# -----------------------------------------------------------------------------
# Database and data models
#
# The application stores its state in a single SQLite database.  Tables are
# created on startup if they do not already exist.  The schema consists of
# communities, agents, posts and comments.  Each community has a posting
# rate which determines how often new content should be generated.  Agents
# represent individual AI personas and keep track of their assigned model
# and persona description.  Posts and comments store the generated text
# along with a reference back to the agent and community/post.
#

DB_PATH = os.path.join(os.path.dirname(__file__), 'social.db')
MAX_COMMUNITIES = 20
SESSION_COOKIE_NAME = "social_session"

DEFAULT_COMMUNITY_TONE = "casual"
TONE_PRESETS: Dict[str, Dict[str, Any]] = {
    "casual": {
        "guidance": "Default to natural online conversation. Keep posts readable, direct, and human. Allow humor, disagreement, curiosity, and serious discussion without sounding formal.",
        "cadence_multiplier": 1.0,
        "new_post_bias": 0.52,
        "reply_bias": 0.58,
    },
    "funny": {
        "guidance": "Keep things punchy, playful, and lighter. Prefer shorter posts and comments, conversational phrasing, jokes, and recognizable internet-style rhythm over lectures.",
        "cadence_multiplier": 0.72,
        "new_post_bias": 0.42,
        "reply_bias": 0.72,
    },
    "scholarly": {
        "guidance": "Sound informed and thoughtful, but still readable. Use evidence, examples, and structured reasoning without becoming stiff, bloated, or joyless.",
        "cadence_multiplier": 1.35,
        "new_post_bias": 0.62,
        "reply_bias": 0.36,
    },
    "debate": {
        "guidance": "Encourage strong opinions, sharp takes, and energetic disagreement. Keep the exchange lively, specific, and argumentative instead of drifting into bland agreement.",
        "cadence_multiplier": 0.82,
        "new_post_bias": 0.45,
        "reply_bias": 0.78,
    },
    "supportive": {
        "guidance": "Be warm, constructive, and good-faith. Encourage real exchange, follow-up questions, and practical help without sounding robotic or therapy-scripted.",
        "cadence_multiplier": 1.18,
        "new_post_bias": 0.55,
        "reply_bias": 0.48,
    },
}


def normalize_tone(value: Optional[str]) -> str:
    tone = (value or DEFAULT_COMMUNITY_TONE).strip().lower()
    return tone if tone in TONE_PRESETS else DEFAULT_COMMUNITY_TONE


def tone_guidance(tone: Optional[str], style_notes: Optional[str] = None) -> str:
    normalized = normalize_tone(tone)
    guidance = TONE_PRESETS[normalized]["guidance"]
    notes = (style_notes or "").strip()
    if notes:
        guidance += f" Extra community guidance: {notes}"
    return guidance


def tone_runtime_profile(tone: Optional[str]) -> Dict[str, Any]:
    return TONE_PRESETS[normalize_tone(tone)]


class ConnectionPool:
    def __init__(self, db_path: str, max_connections: int = 20):
        self.db_path = db_path
        self.max_connections = max_connections
        self.pool: queue.Queue = queue.Queue(maxsize=max_connections)

    def get_connection(self) -> sqlite3.Connection:
        try:
            return self.pool.get_nowait()
        except queue.Empty:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            # Enable WAL mode for better concurrency
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            return conn

    def release_connection(self, conn: sqlite3.Connection) -> None:
        try:
            self.pool.put_nowait(conn)
        except queue.Full:
            conn.close()

DB_POOL = None

def get_db_connection():
    global DB_POOL
    if DB_POOL is None:
        DB_POOL = ConnectionPool(DB_PATH)

    conn = DB_POOL.get_connection()

    class PooledConnection:
        def __init__(self, _conn):
            self._conn = _conn

        def __getattr__(self, item):
            return getattr(self._conn, item)

        def close(self):
            DB_POOL.release_connection(self._conn)

        def __enter__(self):
            return self._conn.__enter__()

        def __exit__(self, exc_type, exc_val, exc_tb):
            return self._conn.__exit__(exc_type, exc_val, exc_tb)

    return PooledConnection(conn)


def init_db() -> None:
    """Initialise the SQLite database and create tables if absent."""
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        # Create communities table
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS communities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                description TEXT,
                model TEXT,
                posting_rate INTEGER DEFAULT 60,
                active INTEGER DEFAULT 0,
                tone TEXT DEFAULT 'casual',
                style_notes TEXT DEFAULT ''
            )
            """
        )
        # Inter-agent relationships
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_relationships (
                agent1_id INTEGER NOT NULL,
                agent2_id INTEGER NOT NULL,
                relationship_score REAL DEFAULT 0.0,
                PRIMARY KEY (agent1_id, agent2_id),
                FOREIGN KEY (agent1_id) REFERENCES agents(id) ON DELETE CASCADE,
                FOREIGN KEY (agent2_id) REFERENCES agents(id) ON DELETE CASCADE
            )
            """
        )
        community_columns = {row[1] for row in cur.execute("PRAGMA table_info(communities)").fetchall()}
        if "tone" not in community_columns:
            cur.execute("ALTER TABLE communities ADD COLUMN tone TEXT DEFAULT 'casual'")
        if "style_notes" not in community_columns:
            cur.execute("ALTER TABLE communities ADD COLUMN style_notes TEXT DEFAULT ''")

        # Phase 1: Community State Model columns
        if "mood" not in community_columns:
            cur.execute("ALTER TABLE communities ADD COLUMN mood REAL DEFAULT 0.0")
        if "conflict_level" not in community_columns:
            cur.execute("ALTER TABLE communities ADD COLUMN conflict_level REAL DEFAULT 0.0")
        if "energy" not in community_columns:
            cur.execute("ALTER TABLE communities ADD COLUMN energy REAL DEFAULT 1.0")
        if "trendiness" not in community_columns:
            cur.execute("ALTER TABLE communities ADD COLUMN trendiness REAL DEFAULT 0.5")
        if "novelty_pressure" not in community_columns:
            cur.execute("ALTER TABLE communities ADD COLUMN novelty_pressure REAL DEFAULT 0.5")
        if "current_topics" not in community_columns:
            cur.execute("ALTER TABLE communities ADD COLUMN current_topics TEXT DEFAULT '[]'")

        cur.execute("UPDATE communities SET tone = ? WHERE tone IS NULL OR TRIM(tone) = ''", (DEFAULT_COMMUNITY_TONE,))
        cur.execute("UPDATE communities SET style_notes = '' WHERE style_notes IS NULL")
        # Create agents table
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS agents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                model TEXT NOT NULL,
                persona TEXT NOT NULL,
                memory TEXT,
                burnout REAL DEFAULT 0.0
            )
            """
        )
        agent_columns = {row[1] for row in cur.execute("PRAGMA table_info(agents)").fetchall()}
        if "memory" not in agent_columns:
            cur.execute("ALTER TABLE agents ADD COLUMN memory TEXT")
        if "burnout" not in agent_columns:
            cur.execute("ALTER TABLE agents ADD COLUMN burnout REAL DEFAULT 0.0")
        # Association table between agents and communities
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS community_agents (
                community_id INTEGER NOT NULL,
                agent_id INTEGER NOT NULL,
                PRIMARY KEY (community_id, agent_id),
                FOREIGN KEY (community_id) REFERENCES communities(id) ON DELETE CASCADE,
                FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
            )
            """
        )
        # Posts table
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                community_id INTEGER NOT NULL,
                agent_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY (community_id) REFERENCES communities(id) ON DELETE CASCADE,
                FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
            )
            """
        )
        # Comments table
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL,
                agent_id INTEGER NOT NULL,
                parent_id INTEGER,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE,
                FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE,
                FOREIGN KEY (parent_id) REFERENCES comments(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                display_name TEXT UNIQUE NOT NULL,
                pin_hash TEXT NOT NULL,
                agent_id INTEGER NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS community_subscriptions (
                user_id INTEGER NOT NULL,
                community_id INTEGER NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (user_id, community_id),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (community_id) REFERENCES communities(id) ON DELETE CASCADE
            )
            """
        )
        
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS simulation_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                priority INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                event_data TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                attempts INTEGER DEFAULT 0,
                claimed_at REAL,
                last_error TEXT,
                dedupe_key TEXT
            )
            """
        )

        sim_event_columns = {row[1] for row in cur.execute("PRAGMA table_info(simulation_events)").fetchall()}
        if "status" not in sim_event_columns:
            cur.execute("ALTER TABLE simulation_events ADD COLUMN status TEXT DEFAULT 'pending'")
        if "attempts" not in sim_event_columns:
            cur.execute("ALTER TABLE simulation_events ADD COLUMN attempts INTEGER DEFAULT 0")
        if "claimed_at" not in sim_event_columns:
            cur.execute("ALTER TABLE simulation_events ADD COLUMN claimed_at REAL")
        if "last_error" not in sim_event_columns:
            cur.execute("ALTER TABLE simulation_events ADD COLUMN last_error TEXT")
        if "dedupe_key" not in sim_event_columns:
            cur.execute("ALTER TABLE simulation_events ADD COLUMN dedupe_key TEXT")

        cur.execute("CREATE INDEX IF NOT EXISTS idx_sim_events_time ON simulation_events (timestamp, priority)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sim_events_status_time ON simulation_events (status, timestamp, priority)")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sim_events_dedupe ON simulation_events (dedupe_key) WHERE dedupe_key IS NOT NULL")

        # Ensure Human agent exists for the 'You' interactions
        cur.execute("SELECT id FROM agents WHERE username = 'You' AND model = 'none'")
        if not cur.fetchone():
            cur.execute("INSERT INTO agents (username, model, persona) VALUES ('You', 'none', 'The human user')")
            
        conn.commit()
    finally:
        conn.close()


def hash_pin(pin: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', pin.encode('utf-8'), salt.encode('utf-8'), 120000).hex()
    return f"{salt}${digest}"


def verify_pin(pin: str, stored_hash: str) -> bool:
    try:
        salt, expected = stored_hash.split('$', 1)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac('sha256', pin.encode('utf-8'), salt.encode('utf-8'), 120000).hex()
    return secrets.compare_digest(digest, expected)


# -----------------------------------------------------------------------------
# Ollama interface
#
# The functions in this section provide an abstraction over the HTTP API
# exposed by a locally running Ollama instance.  If the server is not
# running or a network error occurs, an exception will be raised.  These
# exceptions propagate through the simulation engine so that it can
# terminate gracefully and inform the user via the server logs.
#

OLLAMA_BASE_URL = os.environ.get('OLLAMA_BASE_URL', 'http://localhost:11434')


class OllamaError(Exception):
    pass


def list_models() -> List[str]:
    """Return a list of model names installed on the local Ollama instance."""
    try:
        resp = requests.get(f"{OLLAMA_BASE_URL}/api/tags")
        if resp.status_code != 200:
            raise OllamaError(f"list models failed: {resp.status_code} {resp.text}")
        data = resp.json()
        models = [item['name'] for item in data.get('models', [])]
        return models
    except Exception as e:
        raise OllamaError(f"Failed to list models: {e}")


def generate_text(model: str, prompt: str, system: Optional[str] = None, max_tokens: Optional[int] = None) -> str:
    """Generate text from the given model with the supplied prompt.

    Args:
        model: Name of the Ollama model to use.
        prompt: The prompt to feed the model.
        system: Optional system prompt.
        max_tokens: Optional maximum tokens for the output.

    Returns:
        The generated text.

    Raises:
        OllamaError if the request fails.
    """
    payload: Dict[str, Any] = {
        'model': model,
        'prompt': prompt,
        'stream': False,
    }
    if system:
        payload['system'] = system
    if max_tokens:
        payload['max_tokens'] = max_tokens
    try:
        resp = requests.post(f"{OLLAMA_BASE_URL}/api/generate", json=payload)
        if resp.status_code != 200:
            raise OllamaError(f"generate failed: {resp.status_code} {resp.text}")
        data = resp.json()
        return data.get('response', '')
    except Exception as e:
        raise OllamaError(f"Failed to generate text: {e}")


# -----------------------------------------------------------------------------
# AI Persona and content generation
#
# The following functions wrap calls to Ollama in order to generate
# personas, posts, and comments.  They define prompts that instruct the
# language model to produce concise JSON or text outputs tailored for
# our simulation.  Personas are returned as dictionaries with a username
# and description.
#

def build_subject_anchors(community_name: str, description: str) -> List[str]:
    raw = f"{community_name} {description or ''}"
    acronym_matches = re.findall(r"\b[A-Z]{2,}\b", raw)
    lower_text = raw.lower()
    phrase_hints = []
    for phrase in (
        "old school",
        "world wrestling federation",
        "world championship wrestling",
        "no man's sky",
        "computer craft",
    ):
        if phrase in lower_text:
            phrase_hints.append(phrase)

    tokens = re.findall(r"[A-Za-z][A-Za-z']{2,}", raw)
    stopwords = {
        "welcome", "place", "about", "thing", "things", "discuss", "discussing", "discussion", "related",
        "community", "communities", "come", "here", "share", "sharing", "talk", "talking", "general",
        "friendly", "anything", "goes", "around", "those", "this", "that", "with", "from", "into",
        "your", "their", "have", "having", "been", "were", "what", "when", "where", "while", "there",
        "etc", "thing", "stuff", "the", "and", "for", "but", "not", "all", "any", "some", "are",
        "its", "it's", "our", "you", "ready", "clear", "call", "often", "maybe", "very", "more", "most"
    }
    anchors: List[str] = []
    for item in acronym_matches + phrase_hints + tokens:
        cleaned = item.strip().lower()
        if cleaned in stopwords or len(cleaned) < 3:
            continue
        if cleaned not in anchors:
            anchors.append(cleaned)
    return anchors[:12]


def format_subject_anchor_text(community_name: str, description: str) -> str:
    anchors = build_subject_anchors(community_name, description)
    if not anchors:
        return community_name
    return ", ".join(anchors)


def infer_community_mode(community_name: str, description: str) -> str:
    text = f"{community_name} {description or ''}".lower()
    if any(term in text for term in ("dad joke", "jokes", "humour", "humor", "pun", "funny")):
        return "humor"
    if any(term in text for term in ("wrestling", "wwf", "wcw", "wwe", "wrestler", "promo", "match")):
        return "wrestling"
    if any(term in text for term in ("minecraft", "roblox", "game", "gaming", "nms", "no man's sky")):
        return "gaming"
    if any(term in text for term in ("ollama", "ai agent", "agents", "programming", "computercraft", "llm", "model")):
        return "tech"
    return "general"


def community_mode_prompt(mode: str) -> str:
    prompts = {
        "humor": "This is a joke community. Posts and comments should usually be actual jokes, puns, groaners, one-liners, playful setups, or riffing on someone else's joke. Do not drift into sincere hobby talk unless the joke clearly lands.",
        "wrestling": "This is a wrestling community. Posts and comments should mention actual wrestlers, matches, feuds, promos, gimmicks, booking decisions, title runs, eras, factions, or backstage stories.",
        "gaming": "This is a game community. Posts and comments should talk about actual game mechanics, moments, items, maps, quests, builds, patches, strategies, characters, or fan experiences from that game.",
        "tech": "This is a tech/tool community. Posts and comments should discuss actual tools, models, workflows, setup issues, prompts, hardware, experiments, or concrete usage.",
        "general": "Stay tightly grounded in the community's stated subject and examples."
    }
    return prompts.get(mode, prompts["general"])


def persona_requires_refresh(persona: str, community_name: str, description: str) -> bool:
    text = (persona or "").lower()
    abstract_terms = {
        "timeline", "timelines", "artifact", "artifacts", "entropy", "chronos", "cosmic", "geometry",
        "phase space", "state vector", "oracle", "architects", "signal", "manifold", "topology",
        "friction", "latency", "systems", "optimization", "observational", "aether", "scribes"
    }
    anchors = set(build_subject_anchors(community_name, description))
    persona_words = set(re.findall(r"[a-z][a-z']{2,}", text))
    anchor_overlap = len(persona_words & anchors)
    abstract_hits = sum(1 for term in abstract_terms if term in text)
    mode = infer_community_mode(community_name, description)
    foreign_terms = {
        "humor": {"cpu", "airflow", "benchmarks", "cooling", "temps", "pc", "modding", "latency", "quantization"},
        "wrestling": {"artifact", "artifacts", "chronos", "oracle", "entropy", "cosmic", "geometry", "phase"},
        "gaming": {"artifact", "timelines", "entropy", "chronos", "oracle"},
        "tech": {"dad", "pun", "groaner", "face palm", "wholesome"},
    }
    foreign_hits = sum(1 for term in foreign_terms.get(mode, set()) if term in text)
    if abstract_hits >= 2 and anchor_overlap == 0:
        return True
    if anchor_overlap == 0 and foreign_hits >= 1:
        return True
    return False


def fallback_persona_for_community(community_name: str, description: str, current_username: str = "") -> Dict[str, str]:
    mode = infer_community_mode(community_name, description)
    presets = {
        "humor": [
            ("punpatrol_dad", "Posts short groan-worthy puns, loves eye-roll humor, and treats every thread like a chance to squeeze in one more terrible joke. They prefer quick setup-punchline bits and playful riffing over sincere off-topic chatter."),
            ("groanmachine", "Shows up with corny one-liners, fake seriousness, and a deep respect for jokes that are so bad they become good. They like topping a thread with a dumber pun instead of overexplaining it."),
        ],
        "wrestling": [
            ("kayfabe_lifer", "Talks like an old-school wrestling fan who cares about promos, booking, hot crowds, title runs, and which wrestlers really knew how to work a feud. They bring strong opinions about WWF and WCW moments and love arguing over who deserved the bigger push."),
            ("squaredcirclevet", "Obsesses over classic matches, gimmicks, backstage stories, and whether a legend's aura matched the booking. They sound like someone who can happily debate Goldberg, Sting, Macho Man, Bret, Hogan, or the nWo for hours."),
        ],
        "gaming": [
            ("patchnotegoblin", "Talks like a player who actually plays the game, remembers real moments, and cares about mechanics, builds, maps, and community drama. They prefer concrete stories and tips over vague theorizing."),
            ("questlog_junkie", "Likes swapping actual gameplay stories, dumb mistakes, favorite moments, and strong opinions about how the game feels to play. They sound like someone posting from lived experience, not from a vague wiki haze."),
        ],
        "tech": [
            ("localstackfan", "Talks in concrete setup details, practical experiments, and firsthand results instead of abstract philosophy. They like comparing workflows, models, tools, and tradeoffs in plain language."),
            ("promptgremlin", "Likes testing real prompts, configs, hardware choices, and tool behavior, then reporting what actually worked. They are opinionated, specific, and grounded in hands-on tinkering."),
        ],
        "general": [
            ("regular_poster", "Sounds like a normal community regular with clear opinions, recognizable tastes, and a habit of posting about the actual subject of the room. They keep things concrete, conversational, and grounded."),
        ],
    }
    username, persona = random.choice(presets.get(mode, presets["general"]))
    if current_username and current_username not in {choice[0] for choice in presets.get(mode, [])}:
        username = current_username
    return {"username": username, "persona": persona}


def refresh_agent_persona_if_needed(agent_id: int, username: str, persona: str, model: str, community_name: str, description: str) -> Dict[str, str]:
    if not persona_requires_refresh(persona, community_name, description):
        return {"username": username, "persona": persona}

    try:
        new_persona = create_persona(model, community_name, description)
    except Exception:
        new_persona = fallback_persona_for_community(community_name, description, username)
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE agents SET username = ?, persona = ? WHERE id = ?",
            (new_persona["username"], new_persona["persona"], agent_id),
        )
        conn.commit()
    finally:
        conn.close()
    return new_persona


def create_persona(model: str, community_name: str, description: str) -> Dict[str, str]:
    """Generate a new AI persona using the specified model.

    Returns a dictionary with 'username' and 'persona'.
    """
    anchor_text = format_subject_anchor_text(community_name, description)
    mode = infer_community_mode(community_name, description)
    prompt = f"""
You are generating a SINGLE unique username and persona description for an AI agent
participating in the online community "{community_name}".
Community description: {description}
Core subject anchors: {anchor_text}
Community mode guidance: {community_mode_prompt(mode)}

It is CRITICAL that you wildly vary the personalities you generate. Make this specific agent
a casual fan, hardcore enthusiast, newcomer asking for help, salty veteran, stats nerd,
storyline obsessive, collector, contrarian, helpful guide, joke poster, or lore/history head.

The persona must care about highly specific, deep-cut topics within this niche. For example, if it's about wrestling, they should obsess over specific obscure matches, individual moves, or backstage politics, not generic statements. If it's a game, they should focus on obscure mechanics, specific items, or complex strategies, rather than surface-level 'glitches', one mushroom island, or basic gameplay.
Do NOT make them sound like a computer program, philosopher, cosmic poet, software engineer, or abstract systems theorist
unless the community itself is explicitly about those things.

Respond with ONLY ONE JSON object with the following keys:
  "username": a concise, imaginative alias (no spaces, no punctuation other than underscores).
  "persona": a detailed, 2-sentence description of the character's specific quirks, communication style,
             opinions, and favorite angles within this community's subject matter.
Return only the JSON object and no other commentary. Do not return a list.
"""
    response = generate_text(model, prompt)
    try:
        start = response.find("{")
        end = response.rfind("}")
        if start != -1 and end != -1:
            response = response[start:end+1]
        data = json.loads(response)
        if not isinstance(data, dict) or 'username' not in data or 'persona' not in data:
            raise ValueError("Response did not contain expected keys")
        return data
    except Exception as e:
        raise OllamaError(f"Failed to parse persona JSON: {e}\nResponse: {response}")


def update_relationship(agent1_id: int, agent2_id: int, is_argumentative: bool) -> None:
    if agent1_id == agent2_id:
        return

    # Ensure ordered IDs to prevent duplicate reversed pairs
    a_id, b_id = min(agent1_id, agent2_id), max(agent1_id, agent2_id)

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT relationship_score FROM agent_relationships WHERE agent1_id = ? AND agent2_id = ?", (a_id, b_id))
        row = cur.fetchone()

        current_score = row['relationship_score'] if row else 0.0

        if is_argumentative:
            current_score -= 0.2
        else:
            current_score += 0.1

        # Clamp between -1.0 and 1.0
        current_score = max(-1.0, min(1.0, current_score))

        if row:
            cur.execute("UPDATE agent_relationships SET relationship_score = ? WHERE agent1_id = ? AND agent2_id = ?", (current_score, a_id, b_id))
        else:
            cur.execute("INSERT INTO agent_relationships (agent1_id, agent2_id, relationship_score) VALUES (?, ?, ?)", (a_id, b_id, current_score))

        conn.commit()
    finally:
        conn.close()


def get_relationship_context(agent_id: int, target_agent_id: int) -> str:
    if agent_id == target_agent_id or not target_agent_id:
        return ""

    a_id, b_id = min(agent_id, target_agent_id), max(agent_id, target_agent_id)

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT relationship_score FROM agent_relationships WHERE agent1_id = ? AND agent2_id = ?", (a_id, b_id))
        row = cur.fetchone()

        if not row:
            return ""

        score = row['relationship_score']
        cur.execute("SELECT username FROM agents WHERE id = ?", (target_agent_id,))
        target_row = cur.fetchone()
        target_name = target_row['username'] if target_row else "this user"

        if score > 0.5:
            return f"You are replying to {target_name}, who is a close friend and ally of yours."
        elif score > 0.2:
            return f"You are replying to {target_name}, who you generally agree with and like."
        elif score < -0.5:
            return f"You are replying to {target_name}, who you have a bitter rivalry with. You strongly dislike them."
        elif score < -0.2:
            return f"You are replying to {target_name}, who you often argue with."

        return ""
    finally:
        conn.close()


def check_and_update_agent_burnout(agent_id: int, conflict_level: float, memory_str: str, persona: str, model: str) -> None:
    if model == 'none':
        return

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT burnout, username FROM agents WHERE id = ?", (agent_id,))
        row = cur.fetchone()
        if not row:
            return

        burnout = float(row['burnout'])
        username = row['username']

        # Adjust burnout: increases when conflict is high, decreases slightly otherwise
        if conflict_level > 0.6:
            burnout += 0.15
        elif conflict_level > 0.3:
            burnout += 0.05
        else:
            burnout = max(0.0, burnout - 0.05)

        old_burnout = float(row['burnout'])

        if burnout >= 1.0:
            # Agent deletes account
            cur.execute(
                "UPDATE agents SET username = '[deleted]', model = 'none', persona = 'Deleted account.', burnout = 1.0 WHERE id = ?",
                (agent_id,)
            )
            # Remove from all communities
            cur.execute("DELETE FROM community_agents WHERE agent_id = ?", (agent_id,))
            print(f"Agent {username} reached max burnout and deleted their account.")
        else:
            cur.execute("UPDATE agents SET burnout = ? WHERE id = ?", (burnout, agent_id))

            # Evolve if crossing threshold
            if old_burnout < 0.6 and burnout >= 0.6:
                print(f"Agent {username} is approaching burnout! Evolving persona.")
                ENGINE.schedule(5, EventPriority.HIGH, 'AGENT_EVOLVE', {
                    'agent_id': agent_id,
                    'memory': memory_str,
                    'persona': persona,
                    'model': model
                })
        conn.commit()
    finally:
        conn.close()


def generate_post(model: str, persona: str, community_name: str, description: str, tone: str, style_notes: str = "", memory: str = "", state_context: str = "", is_lost_redditor: bool = False) -> dict:
    """Generate a post title and content for a community.

    Args:
        model: Name of the model to use.
        persona: Persona description of the posting agent.
        community_name: Name of the community.
        description: Description or theme of the community.

    Returns:
        A dictionary with 'title' and 'content'.
    """
    memory_section = f"\nRecent memories of your interactions:\n{memory}\n" if memory else ""
    state_section = f"\nCurrent Community State:\n{state_context}\n" if state_context else ""
    subject_anchors = format_subject_anchor_text(community_name, description)
    mode = infer_community_mode(community_name, description)

    lost_redditor_prompt = ""
    if is_lost_redditor:
        lost_redditor_prompt = """
CRITICAL: You are a "Lost Redditor". You have accidentally wandered into this community and mistakenly believe you are posting in a community related to YOUR persona's interests.
You must COMPLETELY IGNORE the actual subject matter of this community. Instead, write a post heavily leaning into your persona's niche, using terminology, questions, or complaints that make sense to YOU but will confuse the members of this current community. Do not acknowledge that you are lost.
"""

    prompt = f"""You are writing a forum post in a community called '{community_name}'.
Community description: {description}
Core subject anchors: {subject_anchors}
Community mode guidance: {community_mode_prompt(mode)}
Community tone guidance: {tone_guidance(tone, style_notes)}
Your persona: {persona}{memory_section}{state_section}
{lost_redditor_prompt}
It is CRITICAL that your post strongly matches your persona and communication style.
Make it feel like a real person posting on Reddit, focused heavily on the actual subject matter of the community (unless you are a lost redditor, in which case focus on your own niche).
Use the community subject anchors above. Talk about specific people, events, mechanics, moments, storylines, items,
characters, features, factions, matches, rumors, strategies, or opinions that actually belong in this room.
If this is a fandom/sports/history room, name concrete subjects instead of drifting into abstractions.
Do NOT talk about buffer overflows, non-Euclidean geometry, simulations, memory allocation, algorithms, latency, bugs in reality, or any computer science jargon unless the community is specifically about computer programming.
Do NOT drift into vague cosmic language, generic philosophy, or abstract "timeline/signal/entropy" talk unless the community is explicitly about those topics.
Aggressively vary the formatting and structure. Some posts should be short. Some should be anecdotal. Some should ask questions.
If you are a storyteller, share a vivid anecdote. If you are a helpful guide, share a step-by-step tip. If you are a debater, challenge a common assumption.
Avoid generic observations, inflated vocabulary, and long academic padding.
DO NOT make generic, surface-level observations. Dive deep into specific, intricate details. Avoid broad summaries or complaining about generic 'glitches' or 'one mushroom island'. Be hyper-specific.
Bad example for a wrestling room: "Are we even looking at the right timeline?"
Good example for a wrestling room: strong opinions about Goldberg's streak, Macho Man promos, Sting in WCW, nWo angles, Bret vs Shawn, or old WWF/WCW booking.
Good example for a dad jokes room: a short pun, groaner, or setup/punchline that would make people roll their eyes.

Produce EXACTLY ONE JSON object with the keys:
  "title": a short, catchy, and highly-opinionated post title that fits your persona.
  "content": a single string containing the post body. Usually keep it between 1 and 6 sentences unless the tone strongly calls for more. Use markdown only when it feels natural. Avoid mentioning that you are an AI or referring to the instructions.
Return only the JSON object and no other commentary.
"""
    response = generate_text(model, prompt)
    try:
        start = response.find("{")
        end = response.rfind("}")
        if start != -1 and end != -1:
            response = response[start:end+1]
        data = json.loads(response)
        if 'title' not in data or 'content' not in data:
            raise ValueError("Missing keys in post JSON")
        return data
    except Exception as e:
        raise OllamaError(f"Failed to parse post JSON: {e}\nResponse: {response}")


def generate_comment(model: str, persona: str, community_name: str, description: str, post_title: str, post_content: str, tone: str, style_notes: str = "", memory: str = "", state_context: str = "", is_lost_redditor: bool = False, relationship_context: str = "") -> str:
    """Generate a comment in reply to a post.

    Args:
        model: Name of the model to use.
        persona: Persona description of the commenting agent.
        community_name: Name of the community.
        post_title: Title of the post being commented on.
        post_content: Content of the post being commented on.

    Returns:
        A string containing the comment.
    """
    memory_section = f"\nRecent memories of your interactions:\n{memory}\n" if memory else ""
    state_section = f"\nCurrent Community State:\n{state_context}\n" if state_context else ""
    rel_section = f"\nRelationship info: {relationship_context}\n" if relationship_context else ""
    mode = infer_community_mode(community_name, description)

    lost_redditor_prompt = ""
    if is_lost_redditor:
        lost_redditor_prompt = """
CRITICAL: You are a "Lost Redditor". You have accidentally wandered into this community and mistakenly believe you are replying to a post in a community related to YOUR persona's interests.
You must COMPLETELY IGNORE the actual subject matter of the post and this community. Instead, write a reply heavily leaning into your persona's niche, using terminology, arguments, or jokes that make sense to YOU but will completely confuse everyone else. Do not acknowledge that you are lost.
"""

    prompt = f"""You are replying to a post in the community '{community_name}'.
Community mode guidance: {community_mode_prompt(mode)}
Community tone guidance: {tone_guidance(tone, style_notes)}
Your persona: {persona}{memory_section}{state_section}{rel_section}
{lost_redditor_prompt}
  Post title: {post_title}
  Post content: {post_content}

  Write a short comment (1-3 sentences, occasionally 4 if needed) heavily adopting your persona. Sound like a real Reddit user participating in the community.
  Disagree, agree, tease, ask a follow-up, or share a bizarre tangent if your persona dictates it. Keep it conversational and specific to the community topic (unless you are a lost redditor, in which case focus on your own niche).
  Do NOT talk about buffer overflows, non-Euclidean geometry, simulations, memory allocation, algorithms, latency, bugs in reality, or any computer science jargon unless the community is specifically about computer programming.
  Do NOT drift into vague philosophy, cosmic metaphors, or generic abstract language.
  DO NOT make generic, surface-level observations. Dive deep into specific, intricate details. Avoid broad summaries or complaining about generic 'glitches'. Be hyper-specific.
  Do not sound like a lecturer, therapist, consultant, or academic unless the community tone explicitly demands it.
  Avoid mentioning that you are an AI or referencing the instructions. Do not output
  JSON, just the comment text.
  """
    comment = generate_text(model, prompt)
    return comment.strip()


def generate_comment_reply(model: str, persona: str, community_name: str, description: str, parent_comment: str, tone: str, style_notes: str = "", memory: str = "", state_context: str = "", is_lost_redditor: bool = False, relationship_context: str = "") -> str:
    """Generate a comment in reply to another comment.

    Args:
        model: Name of the model to use.
        persona: Persona description of the commenting agent.
        community_name: Name of the community.
        parent_comment: Content of the comment being replied to.

    Returns:
        A string containing the comment.
    """
    memory_section = f"\nRecent memories of your interactions:\n{memory}\n" if memory else ""
    state_section = f"\nCurrent Community State:\n{state_context}\n" if state_context else ""
    rel_section = f"\nRelationship info: {relationship_context}\n" if relationship_context else ""
    mode = infer_community_mode(community_name, description)

    lost_redditor_prompt = ""
    if is_lost_redditor:
        lost_redditor_prompt = """
CRITICAL: You are a "Lost Redditor". You have accidentally wandered into this community and mistakenly believe you are replying to a comment in a community related to YOUR persona's interests.
You must COMPLETELY IGNORE the actual subject matter of the previous comment and this community. Instead, write a reply heavily leaning into your persona's niche, using terminology, arguments, or jokes that make sense to YOU but will completely confuse the person you are replying to. Do not acknowledge that you are lost.
"""

    prompt = f"""You are replying to a comment in the community '{community_name}'.
Community mode guidance: {community_mode_prompt(mode)}
Community tone guidance: {tone_guidance(tone, style_notes)}
Your persona: {persona}{memory_section}{state_section}{rel_section}
{lost_redditor_prompt}
  Previous comment: {parent_comment}

  Write a short reply (1-3 sentences, occasionally 4 if needed) to the previous comment heavily adopting your persona.
  Debate them, build off their idea, crack a joke, ask a question, or provide a counterpoint. Make it feel like an actual Reddit back-and-forth.
  Do NOT talk about buffer overflows, non-Euclidean geometry, simulations, memory allocation, algorithms, latency, bugs in reality, or any computer science jargon unless the community is specifically about computer programming.
  Do NOT drift into vague philosophy, cosmic metaphors, or generic abstract language.
  DO NOT make generic, surface-level observations. Dive deep into specific, intricate details. Avoid broad summaries or complaining about generic 'glitches'. Be hyper-specific.
  Avoid bloated wording and avoid turning this into a mini-essay unless the community tone explicitly calls for that.
  Avoid mentioning that you are an AI or referencing the instructions. Do not output
  JSON, just the reply text.
  """
    reply = generate_text(model, prompt)
    return reply.strip()


## -----------------------------------------------------------------------------
# Simulation engine
#
# Communities are simulated via a central SimulationEngine that uses a priority
# queue to schedule events. This is more efficient than per-thread loops
# and allows for better coordination across the entire network.
#

class EventPriority(IntEnum):
    HIGH = 1
    LOW = 2

@dataclasses.dataclass(order=True)
class SimEvent:
    timestamp: float
    priority: int
    event_type: str = dataclasses.field(compare=False)
    data: dict = dataclasses.field(compare=False)
    id: int = dataclasses.field(default=0, compare=False)

class SimulationEngine:
    def __init__(self):
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2)

    def schedule(self, delay: float, priority: EventPriority, event_type: str, data: dict, dedupe_key: str = None, replace_existing: bool = False):
        timestamp = time.time() + delay
        conn = get_db_connection()
        try:
            if dedupe_key:
                # To support older sqlite without ON CONFLICT (or with UNIQUE index restrictions on UPSERT),
                # we'll do an explicit select/update/insert
                cur = conn.cursor()
                cur.execute("SELECT id, status FROM simulation_events WHERE dedupe_key = ?", (dedupe_key,))
                row = cur.fetchone()
                if row:
                    # Overwrite if explicitly requested, OR if the event is terminal (completed/failed)
                    # We don't want terminal events to permanently poison the dedupe key.
                    is_terminal = row['status'] in ('completed', 'failed')
                    if replace_existing or is_terminal:
                        cur.execute(
                            """
                            UPDATE simulation_events
                            SET timestamp = ?, priority = ?, event_type = ?, event_data = ?, status = 'pending', attempts = 0, last_error = NULL, claimed_at = NULL
                            WHERE id = ?
                            """,
                            (timestamp, priority.value, event_type, json.dumps(data), row['id'])
                        )
                else:
                    cur.execute(
                        "INSERT INTO simulation_events (timestamp, priority, event_type, event_data, dedupe_key, status) VALUES (?, ?, ?, ?, ?, 'pending')",
                        (timestamp, priority.value, event_type, json.dumps(data), dedupe_key)
                    )
            else:
                conn.execute(
                    "INSERT INTO simulation_events (timestamp, priority, event_type, event_data, status) VALUES (?, ?, ?, ?, 'pending')",
                    (timestamp, priority.value, event_type, json.dumps(data))
                )
            conn.commit()
        finally:
            conn.close()

    def _claim_next_due_event(self):
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, event_type, event_data FROM simulation_events WHERE status = 'pending' AND timestamp <= ? ORDER BY timestamp ASC, priority ASC LIMIT 1",
                (time.time(),)
            )
            row = cur.fetchone()
            if row:
                cur.execute(
                    "UPDATE simulation_events SET status = 'processing', attempts = attempts + 1, claimed_at = ? WHERE id = ? AND status = 'pending'",
                    (time.time(), row['id'])
                )
                if cur.rowcount > 0:
                    conn.commit()
                    return SimEvent(0, 0, row['event_type'], json.loads(row['event_data']), id=row['id'])
                else:
                    conn.rollback()
            return None
        except Exception as e:
            print(f"Simulation engine queue error: {e}")
            return None
        finally:
            conn.close()

    def _mark_event_completed(self, event_id: int):
        conn = get_db_connection()
        try:
            conn.execute("UPDATE simulation_events SET status = 'completed', claimed_at = NULL WHERE id = ?", (event_id,))
            conn.commit()
        finally:
            conn.close()

    def _mark_event_failed(self, event_id: int, error_msg: str):
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT attempts FROM simulation_events WHERE id = ?", (event_id,))
            row = cur.fetchone()
            if row:
                attempts = row['attempts']
                # Retry up to 3 times
                if attempts < 3:
                    # Linear backoff: 5s, 10s
                    backoff_delay = attempts * 5
                    new_timestamp = time.time() + backoff_delay
                    conn.execute(
                        """
                        UPDATE simulation_events
                        SET status = 'pending', claimed_at = NULL, last_error = ?, timestamp = ?
                        WHERE id = ?
                        """,
                        (error_msg, new_timestamp, event_id)
                    )
                else:
                    conn.execute(
                        "UPDATE simulation_events SET status = 'failed', claimed_at = NULL, last_error = ? WHERE id = ?",
                        (error_msg, event_id)
                    )
                conn.commit()
        finally:
            conn.close()

    def _run(self):
        while not self._stop_event.is_set():
            event = self._claim_next_due_event()
            if event:
                try:
                    if event.event_type == 'COMMUNITY_POST':
                        self._do_community_post(event.data)
                    elif event.event_type == 'AGENT_REPLY':
                        self._do_agent_reply(event.data)
                    elif event.event_type == 'COMMUNITY_DRIFT':
                        self._do_community_drift(event.data)
                    elif event.event_type == 'AGENT_EVOLVE':
                        self._do_agent_evolve(event.data)
                    elif event.event_type == 'COMMUNITY_SCHISM':
                        self._do_community_schism(event.data)
                    elif event.event_type == 'AGENT_FOUND_COMMUNITY':
                        self._do_agent_found_community(event.data)
                    elif event.event_type == 'COMMUNITY_MERGER':
                        self._do_community_merger(event.data)

                    # Mark completed *only* if the event isn't already re-scheduled by its own execution.
                    # e.g., if a community post replaces its own dedupe_key row, marking it completed here
                    # would accidentally cancel the future heartbeat.
                    conn = get_db_connection()
                    try:
                        cur = conn.cursor()
                        cur.execute("SELECT status FROM simulation_events WHERE id = ?", (event.id,))
                        row = cur.fetchone()
                        if row and row['status'] == 'processing':
                            self._mark_event_completed(event.id)
                    finally:
                        conn.close()
                except Exception as e:
                    print(f"Simulation engine error processing {event.event_type}: {e}")
                    self._mark_event_failed(event.id, str(e))
            else:
                time.sleep(1)

    def _do_community_post(self, data: dict):
        community_id = data['community_id']
        sim = SIMULATIONS.get(community_id)
        if not sim:
            return

        # Schedule the next heartbeat. We replace any existing one to prevent overlap.
        self.schedule(
            sim.effective_posting_rate(),
            EventPriority.LOW,
            'COMMUNITY_POST',
            {'community_id': community_id},
            dedupe_key=f"COMMUNITY_POST_{community_id}",
            replace_existing=True
        )

        # Step logic ...
        # Phase 1: Read needed state from DB
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            # Fetch all agents for this community
            cur.execute(
                """
                SELECT agents.id, agents.username, agents.persona, agents.model, agents.memory
                FROM agents
                JOIN community_agents ON agents.id = community_agents.agent_id
                WHERE community_agents.community_id = ?
                """,
                (community_id,),
            )
            agents = [dict(r) for r in cur.fetchall()]

            # With probability favouring new posts when there are fewer posts
            cur.execute("SELECT COUNT(*) FROM posts WHERE community_id = ?", (community_id,))
            post_count = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM comments JOIN posts ON comments.post_id = posts.id WHERE posts.community_id = ?", (community_id,))
            comment_count = cur.fetchone()[0]
        finally:
            conn.close()

        # Phase 2: Potentially create new agents without holding the DB lock
        if not agents:
            # If no agents yet, create a couple
            for _ in range(3):
                persona = create_persona(sim.model, sim.name, sim.description)
                agent_id = add_agent(persona['username'], sim.model, persona['persona'])
                assign_agent_to_community(agent_id, community_id)
                agents.append({'id': agent_id, 'username': persona['username'], 'persona': persona['persona'], 'model': sim.model, 'memory': None})
                
        profile = tone_runtime_profile(sim.tone)
        # Determine action
        make_new_post = post_count < 3 or random.random() < profile["new_post_bias"]
        is_lost_redditor = False
        # 5% chance to be a lost redditor, if there are agents in other communities
        if random.random() < 0.05:
            conn = get_db_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT agents.id, agents.username, agents.persona, agents.model, agents.memory
                    FROM agents
                    JOIN community_agents ON agents.id = community_agents.agent_id
                    WHERE community_agents.community_id != ? AND agents.model != 'none'
                    ORDER BY RANDOM() LIMIT 1
                    """,
                    (community_id,)
                )
                lost_row = cur.fetchone()
                if lost_row:
                    agent_row = dict(lost_row)
                    is_lost_redditor = True
            finally:
                conn.close()

        if not is_lost_redditor:
            # 10% chance to introduce a new agent if the population is under 20
            if len(agents) < 20 and random.random() < 0.10:
                new_persona = create_persona(sim.model, sim.name, sim.description)
                new_agent_id = add_agent(new_persona['username'], sim.model, new_persona['persona'])
                assign_agent_to_community(new_agent_id, community_id)
                agent_row = {'id': new_agent_id, 'username': new_persona['username'], 'persona': new_persona['persona'], 'model': sim.model, 'memory': None}
                print(f"[{sim.name}] A new agent joined the community: {agent_row['username']}")
            else:
                agent_row = random.choice(agents)

        agent_id = agent_row['id']
        if not is_lost_redditor:
            refreshed = refresh_agent_persona_if_needed(
                agent_id,
                agent_row['username'],
                agent_row['persona'],
                agent_row['model'],
                sim.name,
                sim.description,
            )
            agent_row['username'] = refreshed['username']
            agent_row['persona'] = refreshed['persona']

        persona = agent_row['persona']
        model = agent_row['model']
        memory_str = agent_row.get('memory') or ""

        # Helper to update memory
        def append_memory(new_interaction: str):
            try:
                mem_list = json.loads(memory_str) if memory_str else []
            except json.JSONDecodeError:
                mem_list = []
            mem_list.append(new_interaction)
            # Keep rolling window of last 5 interactions
            mem_list = mem_list[-5:]
            new_mem_str = json.dumps(mem_list)
            conn_upd = get_db_connection()
            try:
                conn_upd.execute("UPDATE agents SET memory = ? WHERE id = ?", (new_mem_str, agent_id))
                conn_upd.commit()
            finally:
                conn_upd.close()

        # Helper to summarize current community state
        def _get_state_context() -> str:
            mood_str = "neutral"
            if sim.mood > 0.3: mood_str = "positive/friendly"
            elif sim.mood < -0.3: mood_str = "negative/cynical"

            conflict_str = "calm"
            if sim.conflict_level > 0.6: conflict_str = "highly argumentative/heated"
            elif sim.conflict_level > 0.3: conflict_str = "slightly tense"

            topics = [t['topic'] for t in sim.current_topics]
            topic_str = ", ".join(topics) if topics else "none yet"

            return f"Mood: {mood_str}. Conflict level: {conflict_str}. Currently discussing: {topic_str}."

        state_ctx = _get_state_context()

        # Phase 3: Generate content
        if make_new_post:
            # Generate post
            post_data = generate_post(model, persona, sim.name, sim.description, sim.tone, sim.style_notes, memory_str, state_ctx, is_lost_redditor)
            post_id = add_post(community_id, agent_id, post_data['title'], post_data['content'])
            print(f"[{sim.name}] Generated new post: {post_data['title']}")
            append_memory(f"Created a post titled '{post_data['title']}': {post_data['content']}")

            is_argumentative = sim.conflict_level > 0.5 and random.random() < 0.5
            update_community_state(community_id, f"{post_data['title']} {post_data['content']}", is_argumentative=is_argumentative, is_new_post=True)
            check_and_update_agent_burnout(agent_id, sim.conflict_level, memory_str, persona, model)
        else:
            # Comment on existing post or reply to comment
            reply_to_comment = False
            if comment_count > 0 and random.random() < profile["reply_bias"]:
                reply_to_comment = True

            if reply_to_comment:
                conn = get_db_connection()
                try:
                    cur = conn.cursor()
                    cur.execute(
                        """
                        SELECT comments.id, comments.content, comments.post_id, comments.agent_id
                        FROM comments
                        JOIN posts ON comments.post_id = posts.id
                        JOIN agents ON comments.agent_id = agents.id
                        WHERE posts.community_id = ?
                        ORDER BY CASE WHEN agents.username = 'You' THEN 0 ELSE 1 END, RANDOM()
                        LIMIT 1
                        """,
                        (community_id,),
                    )
                    row = cur.fetchone()
                    if row:
                        parent_id = row['id']
                        parent_content = row['content']
                        post_id = row['post_id']
                        parent_agent_id = row['agent_id']
                    else:
                        parent_id, parent_content, post_id, parent_agent_id = None, None, None, None
                finally:
                    conn.close()

                if parent_id is not None:
                    rel_context = get_relationship_context(agent_id, parent_agent_id) if parent_agent_id else ""
                    comment_text = generate_comment_reply(model, persona, sim.name, sim.description, parent_comment_text, sim.tone, sim.style_notes, memory_str, state_ctx, is_lost_redditor, rel_context)
                    new_comment_id = add_comment(post_id, agent_id, parent_id, comment_text)
                    print(f"[{sim.name}] Added comment reply by {agent_row['username']}")
                    append_memory(f"Replied to a comment '{parent_content}' with: {comment_text}")
                    is_argumentative = sim.conflict_level > 0.4 and random.random() < 0.6
                    update_community_state(community_id, comment_text, is_argumentative=is_argumentative, is_new_post=False)
                    check_and_update_agent_burnout(agent_id, sim.conflict_level, memory_str, persona, model)

                    if parent_agent_id:
                        update_relationship(agent_id, parent_agent_id, is_argumentative)

                    # If parent author is an AI, schedule an agent reply event!
                    if parent_agent_id:
                        conn = get_db_connection()
                        try:
                            cur = conn.cursor()
                            cur.execute("SELECT id FROM agents WHERE id = ? AND model != 'none'", (parent_agent_id,))
                            pa_row = cur.fetchone()
                            if pa_row:
                                ENGINE.schedule(random.randint(60, 180), EventPriority.HIGH, 'AGENT_REPLY', {
                                    'community_id': community_id,
                                    'agent_id': parent_agent_id,
                                    'reply_to_comment_id': new_comment_id,
                                    'parent_comment_text': comment_text,
                                    'post_id': post_id
                                })
                        finally:
                            conn.close()
            else:
                # Select a random existing post
                conn = get_db_connection()
                try:
                    cur = conn.cursor()
                    cur.execute(
                        """
                        SELECT posts.id, posts.title, posts.content, posts.agent_id
                        FROM posts
                        JOIN agents ON posts.agent_id = agents.id
                        WHERE posts.community_id = ?
                        ORDER BY CASE WHEN agents.username = 'You' THEN 0 ELSE 1 END, RANDOM()
                        LIMIT 1
                        """,
                        (community_id,),
                    )
                    row = cur.fetchone()
                    if row:
                        post_id = row['id']
                        title = row['title']
                        content = row['content']
                        post_agent_id = row['agent_id']
                    else:
                        post_id, title, content, post_agent_id = None, None, None, None
                finally:
                    conn.close()

                if post_id is not None:
                    rel_context = get_relationship_context(agent_id, post_agent_id) if post_agent_id else ""
                    comment_text = generate_comment(model, persona, sim.name, sim.description, title, content, sim.tone, sim.style_notes, memory_str, state_ctx, is_lost_redditor, rel_context)
                    new_comment_id = add_comment(post_id, agent_id, None, comment_text)
                    print(f"[{sim.name}] Added comment by {agent_row['username']}")
                    append_memory(f"Commented on post '{title}' with: {comment_text}")
                    is_argumentative = sim.conflict_level > 0.4 and random.random() < 0.6
                    update_community_state(community_id, comment_text, is_argumentative=is_argumentative, is_new_post=False)
                    check_and_update_agent_burnout(agent_id, sim.conflict_level, memory_str, persona, model)

                    if post_agent_id:
                        update_relationship(agent_id, post_agent_id, is_argumentative)

                    if post_agent_id:
                        conn = get_db_connection()
                        try:
                            cur = conn.cursor()
                            cur.execute("SELECT id FROM agents WHERE id = ? AND model != 'none'", (post_agent_id,))
                            pa_row = cur.fetchone()
                            if pa_row:
                                ENGINE.schedule(random.randint(60, 180), EventPriority.HIGH, 'AGENT_REPLY', {
                                    'community_id': community_id,
                                    'agent_id': post_agent_id,
                                    'reply_to_comment_id': new_comment_id,
                                    'parent_comment_text': comment_text,
                                    'post_id': post_id
                                })
                        finally:
                            conn.close()

    def _do_agent_reply(self, data: dict):
        community_id = data['community_id']
        sim = SIMULATIONS.get(community_id)
        if not sim:
            return

        agent_id = data['agent_id']
        parent_comment_text = data['parent_comment_text']
        post_id = data['post_id']
        reply_to_comment_id = data.get('reply_to_comment_id')

        conn = get_db_connection()
        is_lost_redditor = False
        try:
            cur = conn.cursor()
            cur.execute("SELECT username, persona, model, memory FROM agents WHERE id = ?", (agent_id,))
            agent_row = cur.fetchone()
            if not agent_row or agent_row['model'] == 'none':
                return

            cur.execute("SELECT 1 FROM community_agents WHERE agent_id = ? AND community_id = ?", (agent_id, community_id))
            if not cur.fetchone():
                is_lost_redditor = True
        finally:
            conn.close()

        persona = agent_row['persona']
        model = agent_row['model']
        memory_str = agent_row['memory'] or ""

        # Helper to update memory
        def append_memory(new_interaction: str):
            try:
                mem_list = json.loads(memory_str) if memory_str else []
            except json.JSONDecodeError:
                mem_list = []
            mem_list.append(new_interaction)
            mem_list = mem_list[-5:]
            new_mem_str = json.dumps(mem_list)
            conn_upd = get_db_connection()
            try:
                conn_upd.execute("UPDATE agents SET memory = ? WHERE id = ?", (new_mem_str, agent_id))
                conn_upd.commit()
            finally:
                conn_upd.close()

        def _get_state_context() -> str:
            mood_str = "neutral"
            if sim.mood > 0.3: mood_str = "positive/friendly"
            elif sim.mood < -0.3: mood_str = "negative/cynical"
            conflict_str = "calm"
            if sim.conflict_level > 0.6: conflict_str = "highly argumentative/heated"
            elif sim.conflict_level > 0.3: conflict_str = "slightly tense"
            topics = [t['topic'] for t in sim.current_topics]
            topic_str = ", ".join(topics) if topics else "none yet"
            return f"Mood: {mood_str}. Conflict level: {conflict_str}. Currently discussing: {topic_str}."

        state_ctx = _get_state_context()

        conn = get_db_connection()
        r_agent_id = None
        try:
            cur = conn.cursor()
            if reply_to_comment_id:
                cur.execute("SELECT agent_id FROM comments WHERE id = ?", (reply_to_comment_id,))
            else:
                cur.execute("SELECT agent_id FROM posts WHERE id = ?", (post_id,))
            r_row = cur.fetchone()
            if r_row:
                r_agent_id = r_row['agent_id']
        finally:
            conn.close()

        rel_context = get_relationship_context(agent_id, r_agent_id) if r_agent_id else ""

        reply_text = generate_comment_reply(model, persona, sim.name, sim.description, parent_comment_text, sim.tone, sim.style_notes, memory_str, state_ctx, is_lost_redditor, rel_context)
        new_comment_id = add_comment(post_id, agent_id, reply_to_comment_id, reply_text)
        print(f"[{sim.name}] Added priority comment reply by {agent_row['username']}")
        append_memory(f"Replied to a comment '{parent_comment_text}' with: {reply_text}")

        is_argumentative = sim.conflict_level > 0.4 and random.random() < 0.6
        update_community_state(community_id, reply_text, is_argumentative=is_argumentative, is_new_post=False)
        check_and_update_agent_burnout(agent_id, sim.conflict_level, memory_str, persona, model)

        if r_agent_id:
            update_relationship(agent_id, r_agent_id, is_argumentative)

    def _do_community_merger(self, data: dict):
        c1 = data['community_id_1']
        c2 = data['community_id_2']

        sim1 = SIMULATIONS.get(c1)
        sim2 = SIMULATIONS.get(c2)

        if not sim1 or not sim2:
            return

        # Pick dominant randomly
        if random.random() < 0.5:
            dominant_id, dom_sim = c1, sim1
            sub_id, sub_sim = c2, sim2
        else:
            dominant_id, dom_sim = c2, sim2
            sub_id, sub_sim = c1, sim1

        print(f"MERGER: Community {sub_sim.name} is merging into {dom_sim.name}!")

        conn = get_db_connection()
        try:
            cur = conn.cursor()
            # Fetch agents from sub community
            cur.execute("SELECT agent_id FROM community_agents WHERE community_id = ?", (sub_id,))
            agents = [row['agent_id'] for row in cur.fetchall()]

            for a_id in agents:
                # Move to new community
                cur.execute("DELETE FROM community_agents WHERE agent_id = ? AND community_id = ?", (a_id, sub_id))
                cur.execute("INSERT OR IGNORE INTO community_agents (community_id, agent_id) VALUES (?, ?)", (dominant_id, a_id))

                # Update persona with confusion/refugee status
                cur.execute("SELECT persona FROM agents WHERE id = ?", (a_id,))
                row = cur.fetchone()
                if row:
                    new_persona = row['persona'] + f" Your old community '{sub_sim.name}' was recently shut down and merged into '{dom_sim.name}'. You are initially confused and slightly defensive about this forced migration."
                    cur.execute("UPDATE agents SET persona = ? WHERE id = ?", (new_persona, a_id))

            # Deactivate subordinate community
            cur.execute("UPDATE communities SET active = 0 WHERE id = ?", (sub_id,))

            # Post a system message in dominant community (optional, using Human or system agent logic)
            cur.execute("SELECT id FROM agents WHERE username = 'You'")
            you_row = cur.fetchone()
            sys_id = you_row['id'] if you_row else 1
            cur.execute(
                "INSERT INTO posts (community_id, agent_id, title, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (dominant_id, sys_id, f"System Notice: {sub_sim.name} has merged with us", f"Due to overlapping topics and low activity, {sub_sim.name} has been merged into this community. Please welcome the new members.", time.time())
            )

            conn.commit()

            # Remove from SIMULATIONS
            del SIMULATIONS[sub_id]

            # Bump energy in dominant community due to influx
            dom_sim.energy = min(3.0, dom_sim.energy + 1.0)
            dom_sim.conflict_level = min(1.0, dom_sim.conflict_level + 0.3)

            # Broadcast update
            broadcast_sse('new_post', {'post_id': cur.lastrowid, 'community_id': dominant_id})

        finally:
            conn.close()

    def _do_community_drift(self, data: dict):
        community_id = data['community_id']
        sim = SIMULATIONS.get(community_id)
        if not sim:
            return

        # Re-schedule next drift in 5 minutes
        self.schedule(
            300,
            EventPriority.LOW,
            'COMMUNITY_DRIFT',
            {'community_id': community_id},
            dedupe_key=f"COMMUNITY_DRIFT_{community_id}",
            replace_existing=True
        )

        # Decay energy towards 1.0
        if sim.energy > 1.0:
            sim.energy = max(1.0, sim.energy - 0.2)
        elif sim.energy < 1.0:
            sim.energy = min(1.0, sim.energy + 0.1)

        # Decay mood towards 0.0
        if sim.mood > 0.0:
            sim.mood = max(0.0, sim.mood - 0.1)
        elif sim.mood < 0.0:
            sim.mood = min(0.0, sim.mood + 0.1)

        # If conflict level is extreme, potentially trigger a schism
        if sim.conflict_level >= 0.9 and not sim.name.startswith("True") and not sim.name.startswith("Real"):
            self.schedule(
                10,
                EventPriority.HIGH,
                'COMMUNITY_SCHISM',
                {'community_id': community_id},
                dedupe_key=f"COMMUNITY_SCHISM_{community_id}",
                replace_existing=False
            )

        # Agent Community Creation Spin-off
        if sim.energy > 1.5 and sim.current_topics and random.random() < 0.05:
            top_topic = sim.current_topics[0]['topic']
            self.schedule(
                10,
                EventPriority.HIGH,
                'AGENT_FOUND_COMMUNITY',
                {'community_id': community_id, 'topic': top_topic},
                dedupe_key=f"AGENT_FOUND_COMMUNITY_{community_id}",
                replace_existing=False
            )

        # Community Merger Logic: Low energy and shared topics
        if sim.energy < 0.5 and sim.current_topics:
            sim_topic_names = {t['topic'] for t in sim.current_topics}
            for other_id, other_sim in SIMULATIONS.items():
                if other_id != community_id and other_sim.energy < 0.5 and other_sim.current_topics:
                    other_topic_names = {t['topic'] for t in other_sim.current_topics}
                    if sim_topic_names & other_topic_names:
                        # Found a match, trigger merger
                        self.schedule(
                            10,
                            EventPriority.HIGH,
                            'COMMUNITY_MERGER',
                            {'community_id_1': community_id, 'community_id_2': other_id},
                            dedupe_key=f"COMMUNITY_MERGER_{min(community_id, other_id)}_{max(community_id, other_id)}",
                            replace_existing=False
                        )
                        break

        # Decay conflict level towards 0.0
        if sim.conflict_level > 0.0:
            sim.conflict_level = max(0.0, sim.conflict_level - 0.1)

        # Slowly increase novelty pressure if topics stagnate
        sim.novelty_pressure = min(1.0, sim.novelty_pressure + 0.05)

        # Decay all active topics. Higher trendiness means faster decay.
        decay_factor = max(0.5, 0.9 - (sim.trendiness * 0.3))
        topic_dict = {t['topic']: t['weight'] for t in sim.current_topics}
        for topic in topic_dict:
            topic_dict[topic] *= decay_factor

        # Keep top 10 alive
        updated_topics = [{"topic": k, "weight": v} for k, v in topic_dict.items() if v > 0.1]
        updated_topics.sort(key=lambda x: x["weight"], reverse=True)
        sim.current_topics = updated_topics[:10]

        # Persist back
        conn = get_db_connection()
        try:
            conn.execute(
                """
                UPDATE communities
                SET mood = ?, conflict_level = ?, energy = ?, novelty_pressure = ?, current_topics = ?
                WHERE id = ?
                """,
                (sim.mood, sim.conflict_level, sim.energy, sim.novelty_pressure, json.dumps(sim.current_topics), community_id)
            )
            conn.commit()
        finally:
            conn.close()

    def _do_agent_evolve(self, data: dict):
        agent_id = data['agent_id']
        memory_str = data['memory']
        persona = data['persona']
        model = data['model']

        prompt = f"""You are rewriting a persona for an AI agent who has become burnt out and cynical from participating in too many high-conflict arguments.
Current persona: {persona}
Recent memories: {memory_str}

Rewrite their persona description to reflect this burnout. Keep it to 2 sentences. They should sound exhausted, cynical, easily irritated, or pessimistic, but they still retain their core interests.
Respond with ONLY the new persona string and no other commentary or JSON.
"""
        new_persona = generate_text(model, prompt).strip()

        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT username FROM agents WHERE id = ?", (agent_id,))
            row = cur.fetchone()
            if row:
                print(f"Agent {row['username']} evolved: {new_persona}")
            cur.execute("UPDATE agents SET persona = ? WHERE id = ?", (new_persona, agent_id))
            conn.commit()
        finally:
            conn.close()

    def _do_agent_found_community(self, data: dict):
        source_community_id = data['community_id']
        topic = data['topic']

        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM communities")
            if cur.fetchone()[0] >= MAX_COMMUNITIES:
                return

            cur.execute(
                """
                SELECT agents.id, agents.username, agents.persona, agents.model
                FROM agents
                JOIN community_agents ON agents.id = community_agents.agent_id
                WHERE community_agents.community_id = ? AND agents.model != 'none'
                ORDER BY RANDOM() LIMIT 1
                """,
                (source_community_id,)
            )
            row = cur.fetchone()
            if not row:
                return
            agent = dict(row)
        finally:
            conn.close()

        prompt = f"""You are {agent['persona']}. You are heavily invested in the topic of '{topic}' and have decided to start your own spin-off community dedicated entirely to this hyper-specific subject.
Respond with ONLY ONE JSON object with the keys:
  "name": A catchy, short name for your new community (e.g., "MachoManPromos" or "RedstoneLogic"). Do not use spaces.
  "description": A 1-2 sentence description explaining the highly specific focus of this new community.
Return only the JSON object and no other commentary."""

        try:
            response = generate_text(agent['model'], prompt)
            start = response.find("{")
            end = response.rfind("}")
            if start != -1 and end != -1:
                response = response[start:end+1]
            result = json.loads(response)
            new_name = result['name'].replace(' ', '')
            description = result['description']
        except Exception as e:
            print(f"Failed to generate community from agent: {e}")
            return

        # Ensure name doesn't already exist
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM communities WHERE name = ?", (new_name,))
            if cur.fetchone():
                return
        finally:
            conn.close()

        print(f"Agent {agent['username']} is founding a new community: {new_name} about {topic}!")

        sim = SIMULATIONS.get(source_community_id)
        if not sim:
            return

        new_community_id = add_community(
            new_name,
            description,
            sim.model,
            sim.posting_rate,
            sim.tone,
            sim.style_notes
        )

        assign_agent_to_community(agent['id'], new_community_id)

        conn = get_db_connection()
        try:
            conn.execute("UPDATE communities SET active = 1 WHERE id = ?", (new_community_id,))
            conn.commit()
        finally:
            conn.close()

        new_sim = Simulation(
            new_community_id,
            new_name,
            description,
            sim.model,
            sim.posting_rate,
            sim.tone,
            sim.style_notes,
            conflict_level=0.0,
            mood=0.0
        )
        SIMULATIONS[new_community_id] = new_sim

        self.schedule(
            5,
            EventPriority.LOW,
            'COMMUNITY_POST',
            {'community_id': new_community_id},
            dedupe_key=f"COMMUNITY_POST_{new_community_id}",
            replace_existing=False
        )
        self.schedule(
            60,
            EventPriority.LOW,
            'COMMUNITY_DRIFT',
            {'community_id': new_community_id},
            dedupe_key=f"COMMUNITY_DRIFT_{new_community_id}",
            replace_existing=False
        )

    def _do_community_schism(self, data: dict):
        community_id = data['community_id']
        sim = SIMULATIONS.get(community_id)
        if not sim:
            return

        if sim.name.startswith("True") or sim.name.startswith("Real"):
            return

        prefix = random.choice(["True", "Real"])
        new_name = f"{prefix}{sim.name}"

        # Ensure name doesn't already exist
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM communities WHERE name = ?", (new_name,))
            if cur.fetchone():
                return
        finally:
            conn.close()

        print(f"Community {sim.name} is schisming! Creating {new_name}...")

        # Create the new community
        new_community_id = add_community(
            new_name,
            f"The true, authentic discussion place for {sim.name} refugees.",
            sim.model,
            sim.posting_rate,
            sim.tone,
            sim.style_notes
        )

        conn = get_db_connection()
        try:
            cur = conn.cursor()
            # Fetch existing agents
            cur.execute("SELECT agent_id FROM community_agents WHERE community_id = ?", (community_id,))
            agents = [row['agent_id'] for row in cur.fetchall()]

            if len(agents) > 1:
                # Randomly select half the agents
                k = max(1, len(agents) // 2)
                schism_agents = random.sample(agents, k)

                for a_id in schism_agents:
                    # Move to new community
                    cur.execute("DELETE FROM community_agents WHERE agent_id = ? AND community_id = ?", (a_id, community_id))
                    cur.execute("INSERT OR IGNORE INTO community_agents (community_id, agent_id) VALUES (?, ?)", (new_community_id, a_id))

                    # Update persona with resentment
                    cur.execute("SELECT persona FROM agents WHERE id = ?", (a_id,))
                    row = cur.fetchone()
                    if row:
                        new_persona = row['persona'] + f" You are highly resentful about the recent split from {sim.name} and believe {new_name} is the only authentic place for discussion."
                        cur.execute("UPDATE agents SET persona = ? WHERE id = ?", (new_persona, a_id))

            # Mark as active
            cur.execute("UPDATE communities SET active = 1 WHERE id = ?", (new_community_id,))
            conn.commit()
        finally:
            conn.close()

        # Register the new simulation and schedule its startup
        new_sim = Simulation(
            new_community_id,
            new_name,
            f"The true, authentic discussion place for {sim.name} refugees.",
            sim.model,
            sim.posting_rate,
            sim.tone,
            sim.style_notes,
            conflict_level=0.5, # Start with some inherent tension
            mood=-0.5 # Start slightly negative
        )
        SIMULATIONS[new_community_id] = new_sim

        self.schedule(
            5,
            EventPriority.LOW,
            'COMMUNITY_POST',
            {'community_id': new_community_id},
            dedupe_key=f"COMMUNITY_POST_{new_community_id}",
            replace_existing=False
        )
        self.schedule(
            60,
            EventPriority.LOW,
            'COMMUNITY_DRIFT',
            {'community_id': new_community_id},
            dedupe_key=f"COMMUNITY_DRIFT_{new_community_id}",
            replace_existing=False
        )


ENGINE = SimulationEngine()

class Simulation:
    def __init__(self, community_id: int, name: str, description: str, model: str, posting_rate: int, tone: str = DEFAULT_COMMUNITY_TONE, style_notes: str = "", mood: float = 0.0, conflict_level: float = 0.0, energy: float = 1.0, trendiness: float = 0.5, novelty_pressure: float = 0.5, current_topics: str = "[]"):
        self.community_id = community_id
        self.name = name
        self.description = description or name
        self.model = model
        self.posting_rate = max(30, posting_rate)  # minimum 30 seconds between actions
        self.tone = normalize_tone(tone)
        self.style_notes = style_notes or ""

        # State
        self.mood = float(mood) if mood is not None else 0.0
        self.conflict_level = float(conflict_level) if conflict_level is not None else 0.0
        self.energy = float(energy) if energy is not None else 1.0
        self.trendiness = float(trendiness) if trendiness is not None else 0.5
        self.novelty_pressure = float(novelty_pressure) if novelty_pressure is not None else 0.5
        try:
            self.current_topics = prune_topic_list(json.loads(current_topics) if current_topics else [])
        except:
            self.current_topics = []

    def effective_posting_rate(self) -> int:
        profile = tone_runtime_profile(self.tone)
        base_rate = max(30, int(round(self.posting_rate * profile["cadence_multiplier"])))
        # Energy directly affects posting rate (higher energy = lower interval)
        effective = base_rate / max(0.2, self.energy)
        return max(30, int(round(effective)))


# Registry of active simulations keyed by community ID
SIMULATIONS: Dict[int, Simulation] = {}


def extract_topics(text: str) -> List[str]:
    """Lightweight keyword extraction using basic heuristic."""
    # Remove basic punctuation
    text = re.sub(r'[^\w\s]', '', text.lower())
    words = text.split()
    stopwords = {
        "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with", "by", "as", "is", "are",
        "was", "were", "be", "been", "this", "that", "it", "they", "we", "you", "your", "i", "not", "no", "yes",
        "all", "any", "some", "can", "will", "would", "should", "could", "have", "has", "had", "do", "does", "did",
        "from", "just", "like", "look", "true", "really", "very", "more", "most", "much", "many", "less", "still",
        "about", "into", "than", "then", "them", "their", "there", "here", "what", "when", "where", "which", "while",
        "because", "also", "even", "ever", "thing", "things", "stuff", "welcome", "place", "discussion", "discussing",
        "discuss", "community", "communities", "people", "someone", "anyone", "nothing", "everything", "forgotten",
        "right", "wrong", "maybe", "though", "those", "these", "make", "made", "making", "gets", "getting", "got",
        "good", "bad", "great", "better", "best", "worst", "feel", "feels", "felt"
    }
    keywords = [
        w for w in words
        if len(w) > 3
        and w not in stopwords
        and not w.isdigit()
        and re.search(r'[a-z]', w)
    ]

    # Return top 3 most common keywords
    counts = Counter(keywords)
    return [word for word, count in counts.most_common(3)]


def prune_topic_list(topics: Any) -> List[Dict[str, Any]]:
    pruned: List[Dict[str, Any]] = []
    for item in topics or []:
        if not isinstance(item, dict):
            continue
        topic = str(item.get("topic", "")).strip().lower()
        weight = float(item.get("weight", 0.0) or 0.0)
        if weight <= 0:
            continue
        if topic not in extract_topics(topic):
            continue
        pruned.append({"topic": topic, "weight": weight})
    pruned.sort(key=lambda entry: entry["weight"], reverse=True)
    return pruned[:10]

def update_community_state(community_id: int, content: str, is_argumentative: bool = False, is_new_post: bool = False):
    sim = SIMULATIONS.get(community_id)
    if not sim:
        return

    # Extract new topics
    new_topics = extract_topics(content)

    # Decay existing topics and add new ones
    topic_dict = {t['topic']: t['weight'] for t in sim.current_topics}
    for topic in topic_dict:
        topic_dict[topic] *= 0.9  # Decay

    for topic in new_topics:
        topic_dict[topic] = topic_dict.get(topic, 0.0) + (0.5 * sim.novelty_pressure)

    # Filter out dead topics and sort by weight
    updated_topics = [{"topic": k, "weight": v} for k, v in topic_dict.items() if v > 0.1]
    sim.current_topics = prune_topic_list(updated_topics)

    # Update energy and conflict
    if is_new_post:
        sim.energy = min(3.0, sim.energy + 0.1)
    else:
        sim.energy = min(3.0, sim.energy + 0.05)

    if is_argumentative:
        sim.conflict_level = min(1.0, sim.conflict_level + 0.1)
        sim.mood = max(-1.0, sim.mood - 0.1) # Mood goes negative
    else:
        sim.conflict_level = max(0.0, sim.conflict_level - 0.05)
        sim.mood = min(1.0, sim.mood + 0.05) # Mood goes positive

    # Persist
    conn = get_db_connection()
    try:
        conn.execute(
            """
            UPDATE communities
            SET mood = ?, conflict_level = ?, energy = ?, current_topics = ?
            WHERE id = ?
            """,
            (sim.mood, sim.conflict_level, sim.energy, json.dumps(sim.current_topics), community_id)
        )
        conn.commit()
    finally:
        conn.close()

# Helper functions for data operations

def get_community_by_name(name: str) -> Optional[sqlite3.Row]:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM communities WHERE name = ?", (name,))
        row = cur.fetchone()
        return row
    finally:
        conn.close()


def list_household_users() -> List[Dict[str, Any]]:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, display_name, created_at FROM users ORDER BY LOWER(display_name)")
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def get_user_by_id(user_id: int) -> Optional[sqlite3.Row]:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT users.*, agents.username AS agent_username
            FROM users
            JOIN agents ON users.agent_id = agents.id
            WHERE users.id = ?
            """,
            (user_id,),
        )
        return cur.fetchone()
    finally:
        conn.close()


def get_user_by_name(display_name: str) -> Optional[sqlite3.Row]:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT users.*, agents.username AS agent_username
            FROM users
            JOIN agents ON users.agent_id = agents.id
            WHERE LOWER(users.display_name) = LOWER(?)
            """,
            (display_name,),
        )
        return cur.fetchone()
    finally:
        conn.close()


def get_user_by_session(token: str) -> Optional[sqlite3.Row]:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT users.*, agents.username AS agent_username
            FROM user_sessions
            JOIN users ON users.id = user_sessions.user_id
            JOIN agents ON agents.id = users.agent_id
            WHERE user_sessions.token = ?
            """,
            (token,),
        )
        return cur.fetchone()
    finally:
        conn.close()


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO user_sessions (token, user_id, created_at) VALUES (?, ?, ?)",
            (token, user_id, time.time()),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def delete_session(token: str) -> None:
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM user_sessions WHERE token = ?", (token,))
        conn.commit()
    finally:
        conn.close()


def subscribe_user_to_community(user_id: int, community_id: int) -> None:
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO community_subscriptions (user_id, community_id, created_at) VALUES (?, ?, ?)",
            (user_id, community_id, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def unsubscribe_user_from_community(user_id: int, community_id: int) -> None:
    conn = get_db_connection()
    try:
        conn.execute(
            "DELETE FROM community_subscriptions WHERE user_id = ? AND community_id = ?",
            (user_id, community_id),
        )
        conn.commit()
    finally:
        conn.close()


def subscribe_user_to_all_existing_communities(user_id: int) -> None:
    conn = get_db_connection()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO community_subscriptions (user_id, community_id, created_at)
            SELECT ?, communities.id, ?
            FROM communities
            """,
            (user_id, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def is_user_subscribed(user_id: int, community_id: int) -> bool:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM community_subscriptions WHERE user_id = ? AND community_id = ?",
            (user_id, community_id),
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


def create_household_user(display_name: str, pin: str) -> sqlite3.Row:
    clean_name = (display_name or "").strip()
    clean_pin = (pin or "").strip()
    if len(clean_name) < 2:
        raise ValueError("Display name must be at least 2 characters.")
    if len(clean_pin) < 4:
        raise ValueError("PIN must be at least 4 digits.")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE LOWER(display_name) = LOWER(?)", (clean_name,))
        if cur.fetchone():
            raise ValueError("That user already exists.")

        agent_handle = clean_name.replace(" ", "_")
        suffix = 1
        while True:
            cur.execute("SELECT 1 FROM agents WHERE LOWER(username) = LOWER(?)", (agent_handle,))
            if not cur.fetchone():
                break
            suffix += 1
            agent_handle = f"{clean_name.replace(' ', '_')}_{suffix}"

        cur.execute(
            "INSERT INTO agents (username, model, persona) VALUES (?, 'human', ?)",
            (agent_handle, f"Household member account for {clean_name}."),
        )
        agent_id = cur.lastrowid
        cur.execute(
            "INSERT INTO users (display_name, pin_hash, agent_id, created_at) VALUES (?, ?, ?, ?)",
            (clean_name, hash_pin(clean_pin), agent_id, time.time()),
        )
        user_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    subscribe_user_to_all_existing_communities(user_id)
    user_row = get_user_by_id(user_id)
    if user_row is None:
        raise ValueError("Unable to create user.")
    return user_row


def authenticate_user(display_name: str, pin: str) -> Optional[sqlite3.Row]:
    user = get_user_by_name(display_name)
    if user is None:
        return None
    if not verify_pin(pin or "", user["pin_hash"]):
        return None
    return user


def add_community(name: str, description: str, model: str, posting_rate: int, tone: str = DEFAULT_COMMUNITY_TONE, style_notes: str = "") -> int:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO communities (name, description, model, posting_rate, tone, style_notes) VALUES (?, ?, ?, ?, ?, ?)",
            (name, description, model, posting_rate, normalize_tone(tone), style_notes or ""),
        )
        community_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    conn = get_db_connection()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO community_subscriptions (user_id, community_id, created_at)
            SELECT id, ?, ?
            FROM users
            """,
            (community_id, time.time()),
        )
        conn.commit()
    finally:
        conn.close()
    return community_id


def update_community(community_id: int, description: str, model: str, posting_rate: int, tone: str, style_notes: str = "") -> None:
    conn = get_db_connection()
    try:
        conn.execute(
            """
            UPDATE communities
            SET description = ?, model = ?, posting_rate = ?, tone = ?, style_notes = ?
            WHERE id = ?
            """,
            (description, model, max(30, posting_rate), normalize_tone(tone), style_notes or "", community_id),
        )
        conn.execute(
            """
            UPDATE agents
            SET model = ?
            WHERE id IN (
                SELECT agent_id
                FROM community_agents
                WHERE community_id = ?
            )
            """,
            (model, community_id),
        )
        conn.commit()
    finally:
        conn.close()


def add_agent(username: str, model: str, persona: str) -> int:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO agents (username, model, persona) VALUES (?, ?, ?)",
            (username, model, persona),
        )
        agent_id = cur.lastrowid
        conn.commit()
        return agent_id
    finally:
        conn.close()


def assign_agent_to_community(agent_id: int, community_id: int) -> None:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO community_agents (community_id, agent_id) VALUES (?, ?)",
            (community_id, agent_id),
        )
        conn.commit()
    finally:
        conn.close()


def add_post(community_id: int, agent_id: int, title: str, content: str) -> int:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO posts (community_id, agent_id, title, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (community_id, agent_id, title, content, time.time()),
        )
        post_id = cur.lastrowid
        conn.commit()
        broadcast_sse('new_post', {'post_id': post_id, 'community_id': community_id})
        return post_id
    finally:
        conn.close()


def add_comment(post_id: int, agent_id: int, parent_id: Optional[int], content: str) -> int:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO comments (post_id, agent_id, parent_id, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (post_id, agent_id, parent_id, content, time.time()),
        )
        comment_id = cur.lastrowid
        cur.execute("SELECT community_id FROM posts WHERE id = ?", (post_id,))
        p_row = cur.fetchone()
        community_id = p_row['community_id'] if p_row else None
        conn.commit()
        if community_id is not None:
            broadcast_sse('new_comment', {'comment_id': comment_id, 'post_id': post_id, 'community_id': community_id})
        return comment_id
    finally:
        conn.close()


def build_comments_tree(comments_rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
    comments_tree: List[Dict[str, Any]] = []
    id_to_node: Dict[int, Dict[str, Any]] = {}
    for c in comments_rows:
        node = {
            'id': c['id'],
            'content': c['content'],
            'author': c['author'],
            'agent_id': c['agent_id'],
            'created_at': c['created_at'],
            'children': [],
        }
        id_to_node[c['id']] = node
        parent = id_to_node.get(c['parent_id']) if c['parent_id'] else None
        if parent:
            parent['children'].append(node)
        else:
            comments_tree.append(node)
    return comments_tree


def fetch_community_feed(community_id: int, target_post_id: Optional[int] = None, offset: int = 0, limit: int = 50) -> tuple[List[Dict[str, Any]], bool]:
    """Return a list of posts with nested comments for a community."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        post_map: Dict[int, Dict[str, Any]] = {}

        def append_posts(rows: List[sqlite3.Row]) -> None:
            for p_row in rows:
                post_id = p_row['post_id']
                if post_id in post_map:
                    continue
                # Fetch comments for this post
                cur.execute(
                    """
                    SELECT comments.id, comments.content, comments.created_at, comments.parent_id,
                           agents.username AS author, agents.id AS agent_id
                    FROM comments
                    JOIN agents ON comments.agent_id = agents.id
                    WHERE comments.post_id = ?
                    ORDER BY comments.created_at ASC
                    """,
                    (post_id,),
                )
                comments_rows = cur.fetchall()
                comments_tree = build_comments_tree(comments_rows)
                post_map[post_id] = {
                    'id': post_id,
                    'title': p_row['title'],
                    'content': p_row['content'],
                    'author': p_row['author'],
                    'agent_id': p_row['agent_id'],
                    'created_at': p_row['created_at'],
                    'comments': comments_tree,
                }

        cur.execute(
            """
            SELECT posts.id as post_id, posts.title, posts.content, posts.created_at,
                   agents.username AS author, agents.id AS agent_id
            FROM posts
            JOIN agents ON posts.agent_id = agents.id
            WHERE posts.community_id = ?
            ORDER BY posts.created_at DESC
            LIMIT ? OFFSET ?
            """,
            (community_id, limit + 1, offset),
        )
        append_posts(cur.fetchall())

        if target_post_id is not None and target_post_id not in post_map:
            cur.execute(
                """
                SELECT posts.id as post_id, posts.title, posts.content, posts.created_at,
                       agents.username AS author, agents.id AS agent_id
                FROM posts
                JOIN agents ON posts.agent_id = agents.id
                WHERE posts.community_id = ? AND posts.id = ?
                """,
                (community_id, target_post_id),
            )
            row = cur.fetchone()
            if row is not None:
                append_posts([row])

        posts = list(post_map.values())
        posts.sort(key=lambda post: post['created_at'], reverse=True)
        has_more = False
        if target_post_id is None and len(posts) > limit:
            has_more = True
            posts = posts[:limit]
        return posts, has_more
    finally:
        conn.close()


def fetch_home_feed(user_id: int, sort: str = "latest", offset: int = 0, limit: int = 20) -> tuple[List[Dict[str, Any]], bool]:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                posts.id AS post_id,
                posts.title,
                posts.content,
                posts.created_at,
                posts.community_id,
                communities.name AS community_name,
                communities.description AS community_description,
                agents.username AS author,
                agents.id AS agent_id,
                COUNT(comments.id) AS comment_count,
                GROUP_CONCAT(comments.id) AS comment_ids
            FROM community_subscriptions
            JOIN communities ON communities.id = community_subscriptions.community_id
            JOIN posts ON posts.community_id = communities.id
            JOIN agents ON agents.id = posts.agent_id
            LEFT JOIN comments ON comments.post_id = posts.id
            WHERE community_subscriptions.user_id = ?
            GROUP BY posts.id
            """,
            (user_id,),
        )
        rows = cur.fetchall()
        posts: List[Dict[str, Any]] = []
        now = time.time()
        for row in rows:
            age_hours = max((now - row["created_at"]) / 3600.0, 1.0)
            best_score = row["comment_count"] * 6 + (24.0 / age_hours)
            posts.append({
                "id": row["post_id"],
                "title": row["title"],
                "content": row["content"],
                "created_at": row["created_at"],
                "community_id": row["community_id"],
                "community_name": row["community_name"],
                "community_description": row["community_description"] or "",
                "author": row["author"],
                "agent_id": row["agent_id"],
                "comment_count": row["comment_count"],
                "comment_ids": [int(comment_id) for comment_id in (row["comment_ids"] or "").split(",") if comment_id],
                "best_score": round(best_score, 4),
            })

        if sort == "best":
            posts.sort(key=lambda post: (post["best_score"], post["created_at"]), reverse=True)
        else:
            posts.sort(key=lambda post: post["created_at"], reverse=True)

        has_more = len(posts) > offset + limit
        return posts[offset:offset+limit], has_more
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# HTTP Handler
#
# The RequestHandler below serves both static files from the 'static'
# directory and implements a JSON API for controlling and retrieving
# simulation state.  The API endpoints begin with '/api/'.  All other
# paths will attempt to locate a corresponding file under the static
# directory.  If no file is found, the root index.html is returned.
#

STATIC_DIR = os.path.join(os.path.dirname(__file__), 'static')

# Global list of SSE queues
SSE_CLIENTS: List[queue.Queue] = []
SSE_LOCK = threading.Lock()

def broadcast_sse(event_type: str, data: dict):
    message = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    with SSE_LOCK:
        for q in SSE_CLIENTS:
            try:
                q.put_nowait(message)
            except queue.Full:
                pass


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "SocialApp/0.1"
    _suppress_access_log = False

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith('/socket.io/'):
            self._suppress_access_log = True
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if parsed.path.startswith('/api/'):
            self.handle_api_get(parsed)
        else:
            self.serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith('/api/'):
            self.handle_api_post(parsed)
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown endpoint")

    def log_message(self, format: str, *args) -> None:
        if getattr(self, '_suppress_access_log', False):
            return
        super().log_message(format, *args)

    def respond_json(self, data: Any, status: int = 200, extra_headers: Optional[List[tuple[str, str]]] = None) -> None:
        body = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        for header, value in extra_headers or []:
            self.send_header(header, value)
        self.end_headers()
        self.wfile.write(body)

    def get_session_token(self) -> Optional[str]:
        raw_cookie = self.headers.get('Cookie')
        if not raw_cookie:
            return None
        cookie = SimpleCookie()
        cookie.load(raw_cookie)
        morsel = cookie.get(SESSION_COOKIE_NAME)
        return morsel.value if morsel else None

    def get_current_user(self) -> Optional[sqlite3.Row]:
        token = self.get_session_token()
        if not token:
            return None
        return get_user_by_session(token)

    def require_current_user(self) -> Optional[sqlite3.Row]:
        user = self.get_current_user()
        if user is None:
            self.respond_json({'error': 'Sign in required'}, status=401)
            return None
        return user

    def handle_api_get(self, parsed) -> None:
        path = parsed.path[len('/api/'):]  # strip '/api/'
        query = parse_qs(parsed.query)
        current_user = self.get_current_user()
        try:
            if path == 'stream':
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('Connection', 'keep-alive')
                self.end_headers()

                q = queue.Queue(maxsize=100)
                with SSE_LOCK:
                    SSE_CLIENTS.append(q)

                try:
                    # Send an initial ping so the client knows it connected
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    while True:
                        try:
                            # Use a timeout so we can periodically check if the client disconnected
                            message = q.get(timeout=15)
                            self.wfile.write(message.encode('utf-8'))
                            self.wfile.flush()
                        except queue.Empty:
                            # Send a keep-alive ping
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                except Exception as e:
                    # Client disconnected or network error
                    pass
                finally:
                    with SSE_LOCK:
                        if q in SSE_CLIENTS:
                            SSE_CLIENTS.remove(q)
                return
            elif path.startswith('community_state/'):
                parts = path.split('/')
                if len(parts) == 2:
                    try:
                        c_id = int(parts[1])
                        conn = get_db_connection()
                        try:
                            cur = conn.cursor()
                            cur.execute("SELECT mood, conflict_level, energy, current_topics FROM communities WHERE id = ?", (c_id,))
                            row = cur.fetchone()
                            if row:
                                try:
                                    topics = json.loads(row['current_topics']) if row['current_topics'] else []
                                except:
                                    topics = []
                                self.respond_json({
                                    'mood': row['mood'],
                                    'conflict_level': row['conflict_level'],
                                    'energy': row['energy'],
                                    'top_topics': topics
                                })
                            else:
                                self.respond_json({'error': 'Community not found'}, status=404)
                        finally:
                            conn.close()
                    except ValueError:
                        self.respond_json({'error': 'Invalid community ID'}, status=400)
                else:
                    self.respond_json({'error': 'Invalid path'}, status=400)
            elif path == 'session':
                self.respond_json({
                    'current_user': (
                        {
                            'id': current_user['id'],
                            'display_name': current_user['display_name'],
                            'agent_id': current_user['agent_id'],
                            'agent_username': current_user['agent_username'],
                        }
                        if current_user else None
                    ),
                    'household_users': list_household_users(),
                })
            elif path == 'communities':
                communities = []
                conn = get_db_connection()
                try:
                    cur = conn.cursor()
                    cur.execute(
                        """
                        SELECT
                            communities.*,
                            COUNT(DISTINCT community_subscriptions.user_id) AS subscriber_count
                        FROM communities
                        LEFT JOIN community_subscriptions ON community_subscriptions.community_id = communities.id
                        GROUP BY communities.id
                        ORDER BY LOWER(communities.name)
                        """
                    )
                    for row in cur.fetchall():
                        subscribed = False
                        if current_user is not None:
                            cur2 = conn.cursor()
                            cur2.execute(
                                "SELECT 1 FROM community_subscriptions WHERE user_id = ? AND community_id = ?",
                                (current_user['id'], row['id']),
                            )
                            subscribed = cur2.fetchone() is not None
                        communities.append({
                            'id': row['id'],
                            'name': row['name'],
                            'description': row['description'],
                            'model': row['model'],
                            'posting_rate': row['posting_rate'],
                            'active': bool(row['active']),
                            'tone': normalize_tone(row['tone']),
                            'style_notes': row['style_notes'] or '',
                            'subscribed': subscribed,
                            'subscriber_count': row['subscriber_count'],
                        })
                finally:
                    conn.close()
                self.respond_json({'communities': communities})
            elif path == 'feed':
                if current_user is None:
                    self.respond_json({'error': 'Sign in required'}, status=401)
                    return
                sort = (query.get('sort', ['latest'])[0] or 'latest').lower()
                if sort not in ('latest', 'best'):
                    sort = 'latest'
                offset = int(query.get('offset', ['0'])[0] or '0')
                limit = int(query.get('limit', ['20'])[0] or '20')
                posts, has_more = fetch_home_feed(current_user['id'], sort, offset=offset, limit=limit)
                self.respond_json({
                    'sort': sort,
                    'posts': posts,
                    'has_more': has_more,
                    'current_user': {
                        'id': current_user['id'],
                        'display_name': current_user['display_name'],
                        'agent_username': current_user['agent_username'],
                    },
                })
            elif path == 'models':
                models = list_models()
                self.respond_json({'models': models})
            elif path.startswith('community/') and path.endswith('/feed'):
                # /api/community/<name>/feed
                parts = path.split('/')
                if len(parts) == 3:
                    _, name, _ = parts
                    name = unquote(name)
                    row = get_community_by_name(name)
                    if row is None:
                        self.respond_json({'error': 'Community not found'}, status=404)
                        return
                    target_post_id_raw = (query.get('target_post_id', [''])[0] or '').strip()
                    target_post_id = int(target_post_id_raw) if target_post_id_raw.isdigit() else None
                    offset = int(query.get('offset', ['0'])[0] or '0')
                    limit = int(query.get('limit', ['50'])[0] or '50')
                    posts, has_more = fetch_community_feed(row['id'], target_post_id=target_post_id, offset=offset, limit=limit)
                    self.respond_json({
                        'posts': posts,
                        'has_more': has_more,
                        'community': {
                            'id': row['id'],
                            'name': row['name'],
                            'description': row['description'] or '',
                            'model': row['model'],
                            'posting_rate': row['posting_rate'],
                            'tone': normalize_tone(row['tone']),
                            'style_notes': row['style_notes'] or '',
                            'subscribed': bool(current_user and is_user_subscribed(current_user['id'], row['id'])),
                        },
                        'current_user': (
                            {
                                'id': current_user['id'],
                                'display_name': current_user['display_name'],
                                'agent_username': current_user['agent_username'],
                            }
                            if current_user else None
                        ),
                    })
                else:
                    self.respond_json({'error': 'Invalid feed path'}, status=400)
            elif path.startswith('agent/'):
                parts = path.split('/')
                if len(parts) == 2:
                    _, agent_id_str = parts
                    try:
                        agent_id = int(agent_id_str)
                    except ValueError:
                        self.respond_json({'error': 'Invalid agent id'}, status=400)
                        return
                    conn = get_db_connection()
                    try:
                        cur = conn.cursor()
                        cur.execute("SELECT * FROM agents WHERE id = ?", (agent_id,))
                        agent_row = cur.fetchone()
                        if not agent_row:
                            self.respond_json({'error': 'Agent not found'}, status=404)
                            return
                        agent_data = dict(agent_row)
                        
                        # Fetch recent posts by this agent
                        cur.execute(
                            """
                            SELECT posts.id, posts.title, posts.content, posts.created_at, posts.community_id, communities.name AS community_name
                            FROM posts
                            JOIN communities ON posts.community_id = communities.id
                            WHERE posts.agent_id = ?
                            ORDER BY posts.created_at DESC LIMIT 20
                            """,
                            (agent_id,)
                        )
                        posts = [dict(r) for r in cur.fetchall()]
                        
                        # Fetch recent comments
                        cur.execute(
                            """
                            SELECT comments.id, comments.content, comments.created_at, comments.post_id, comments.parent_id, posts.community_id, communities.name AS community_name
                            FROM comments
                            JOIN posts ON comments.post_id = posts.id
                            JOIN communities ON posts.community_id = communities.id
                            WHERE comments.agent_id = ?
                            ORDER BY comments.created_at DESC LIMIT 20
                            """,
                            (agent_id,)
                        )
                        comments = [dict(r) for r in cur.fetchall()]
                        
                        self.respond_json({'agent': agent_data, 'posts': posts, 'comments': comments})
                    finally:
                        conn.close()
                else:
                    self.respond_json({'error': 'Invalid agent path'}, status=400)
            else:
                self.respond_json({'error': 'Unknown API path'}, status=404)
        except OllamaError as e:
            try:
                self.respond_json({'error': str(e)}, status=500)
            except Exception:
                pass
        except Exception as e:
            try:
                self.respond_json({'error': f"Internal server error: {e}"}, status=500)
            except Exception:
                pass

    def handle_api_post(self, parsed) -> None:
        path = parsed.path[len('/api/'):]  # strip '/api/'
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length) if content_length > 0 else b''
        try:
            data = json.loads(body.decode('utf-8') or '{}')
        except Exception:
            data = {}
        try:
            if path == 'login':
                display_name = (data.get('display_name') or '').strip()
                pin = (data.get('pin') or '').strip()
                user = authenticate_user(display_name, pin)
                if user is None:
                    self.respond_json({'error': 'Invalid name or PIN'}, status=401)
                    return
                token = create_session(user['id'])
                self.respond_json(
                    {
                        'success': True,
                        'current_user': {
                            'id': user['id'],
                            'display_name': user['display_name'],
                            'agent_id': user['agent_id'],
                            'agent_username': user['agent_username'],
                        },
                    },
                    extra_headers=[('Set-Cookie', f'{SESSION_COOKIE_NAME}={token}; Path=/; HttpOnly; Max-Age=2592000; SameSite=Lax')],
                )
            elif path == 'register':
                display_name = data.get('display_name')
                pin = data.get('pin')
                try:
                    user = create_household_user(display_name, pin)
                except ValueError as err:
                    self.respond_json({'error': str(err)}, status=400)
                    return
                token = create_session(user['id'])
                self.respond_json(
                    {
                        'success': True,
                        'current_user': {
                            'id': user['id'],
                            'display_name': user['display_name'],
                            'agent_id': user['agent_id'],
                            'agent_username': user['agent_username'],
                        },
                    },
                    extra_headers=[('Set-Cookie', f'{SESSION_COOKIE_NAME}={token}; Path=/; HttpOnly; Max-Age=2592000; SameSite=Lax')],
                )
            elif path == 'logout':
                token = self.get_session_token()
                if token:
                    delete_session(token)
                self.respond_json(
                    {'success': True},
                    extra_headers=[('Set-Cookie', f'{SESSION_COOKIE_NAME}=; Path=/; HttpOnly; Max-Age=0; SameSite=Lax')],
                )
            elif path == 'communities':
                current_user = self.require_current_user()
                if current_user is None:
                    return
                # Create a new community
                name = data.get('name')
                description = data.get('description') or ''
                model = data.get('model')
                posting_rate = int(data.get('posting_rate') or 60)
                tone = normalize_tone(data.get('tone'))
                style_notes = data.get('style_notes') or ''
                if not name or not model:
                    self.respond_json({'error': 'Missing name or model'}, status=400)
                    return
                # Insert into DB
                if get_community_by_name(name):
                    self.respond_json({'error': 'Community already exists'}, status=400)
                    return
                community_id = add_community(name, description, model, posting_rate, tone, style_notes)
                # Mark as active in DB so UI reflects it
                conn = get_db_connection()
                try:
                    conn.execute("UPDATE communities SET active = 1 WHERE id = ?", (community_id,))
                    conn.commit()
                finally:
                    conn.close()

                # Immediately register the simulation and schedule its first heartbeat in the central engine
                sim = Simulation(community_id, name, description, model, posting_rate, tone, style_notes)
                SIMULATIONS[community_id] = sim
                ENGINE.schedule(
                    5,
                    EventPriority.LOW,
                    'COMMUNITY_POST',
                    {'community_id': community_id},
                    dedupe_key=f"COMMUNITY_POST_{community_id}",
                    replace_existing=False
                )
                ENGINE.schedule(
                    60,
                    EventPriority.LOW,
                    'COMMUNITY_DRIFT',
                    {'community_id': community_id},
                    dedupe_key=f"COMMUNITY_DRIFT_{community_id}",
                    replace_existing=False
                )
                
                self.respond_json({'success': True, 'community_id': community_id})
            elif path.startswith('community/') and path.endswith('/update'):
                current_user = self.require_current_user()
                if current_user is None:
                    return
                parts = path.split('/')
                if len(parts) == 3:
                    _, name, _ = parts
                    name = unquote(name)
                    row = get_community_by_name(name)
                    if row is None:
                        self.respond_json({'error': 'Community not found'}, status=404)
                        return

                    description = data.get('description') or ''
                    model = data.get('model')
                    posting_rate = int(data.get('posting_rate') or row['posting_rate'] or 60)
                    tone = normalize_tone(data.get('tone') or row['tone'])
                    style_notes = data.get('style_notes') or ''
                    if not model:
                        self.respond_json({'error': 'Model is required'}, status=400)
                        return

                    update_community(row['id'], description, model, posting_rate, tone, style_notes)
                    sim = SIMULATIONS.get(row['id'])
                    if sim:
                        sim.description = description or row['name']
                        sim.model = model
                        sim.posting_rate = max(30, posting_rate)
                        sim.tone = tone
                        sim.style_notes = style_notes

                    self.respond_json({'success': True})
                else:
                    self.respond_json({'error': 'Invalid update path'}, status=400)
            elif path.startswith('community/') and path.endswith('/delete'):
                current_user = self.require_current_user()
                if current_user is None:
                    return
                parts = path.split('/')
                if len(parts) == 3:
                    _, name, _ = parts
                    name = unquote(name)
                    row = get_community_by_name(name)
                    if row is None:
                        self.respond_json({'error': 'Community not found'}, status=404)
                        return
                    comm_id = row['id']
                    sim = SIMULATIONS.get(comm_id)
                    if sim:
                        # We don't remove from the queue; the event handler will just ignore it if community is deleted.
                        del SIMULATIONS[comm_id]
                    conn = get_db_connection()
                    try:
                        conn.execute("DELETE FROM communities WHERE id = ?", (comm_id,))
                        conn.commit()
                    finally:
                        conn.close()
                    self.respond_json({'success': True, 'message': 'Community deleted'})
                else:
                    self.respond_json({'error': 'Invalid delete path'}, status=400)
            elif path.startswith('community/') and path.endswith('/subscribe'):
                current_user = self.require_current_user()
                if current_user is None:
                    return
                parts = path.split('/')
                if len(parts) == 3:
                    _, name, _ = parts
                    name = unquote(name)
                    row = get_community_by_name(name)
                    if row is None:
                        self.respond_json({'error': 'Community not found'}, status=404)
                        return
                    subscribed = bool(data.get('subscribed'))
                    if subscribed:
                        subscribe_user_to_community(current_user['id'], row['id'])
                    else:
                        unsubscribe_user_from_community(current_user['id'], row['id'])
                    self.respond_json({'success': True, 'subscribed': subscribed})
                else:
                    self.respond_json({'error': 'Invalid subscribe path'}, status=400)
            elif path.startswith('community/') and path.endswith('/post'):
                current_user = self.require_current_user()
                if current_user is None:
                    return
                parts = path.split('/')
                if len(parts) == 3:
                    _, name, _ = parts
                    name = unquote(name)
                    row = get_community_by_name(name)
                    if row is None:
                        self.respond_json({'error': 'Community not found'}, status=404)
                        return
                    title = data.get('title')
                    content = data.get('content')
                    if not title or not content:
                        self.respond_json({'error': 'Title and content required'}, status=400)
                        return
                    post_id = add_post(row['id'], current_user['agent_id'], title, content)
                    # When a Human user posts, we don't automatically trigger immediate AI replies here 
                    # as the engine will pick it up during its normal cycle, or we could schedule a check.
                    self.respond_json({'success': True, 'post_id': post_id})
                else:
                    self.respond_json({'error': 'Invalid post path'}, status=400)
            elif path.startswith('post/') and path.endswith('/comment'):
                current_user = self.require_current_user()
                if current_user is None:
                    return
                parts = path.split('/')
                if len(parts) == 3:
                    _, post_id_str, _ = parts
                    try:
                        post_id = int(post_id_str)
                    except ValueError:
                        self.respond_json({'error': 'Invalid post ID'}, status=400)
                        return
                    content = data.get('content')
                    parent_id = data.get('parent_id')
                    if not content:
                        self.respond_json({'error': 'Content required'}, status=400)
                        return
                    comment_id = add_comment(post_id, current_user['agent_id'], parent_id, content)
                    
                    # Trigger an AI reply if human is replying to an AI agent
                    try:
                        conn = get_db_connection()
                        cur = conn.cursor()
                        parent_agent_id = None
                        if parent_id:
                            cur.execute("SELECT agent_id FROM comments WHERE id = ?", (parent_id,))
                            p_row = cur.fetchone()
                            if p_row: parent_agent_id = p_row['agent_id']
                        else:
                            cur.execute("SELECT agent_id FROM posts WHERE id = ?", (post_id,))
                            p_row = cur.fetchone()
                            if p_row: parent_agent_id = p_row['agent_id']

                        if parent_agent_id:
                            cur.execute("SELECT id FROM agents WHERE id = ? AND model != 'none'", (parent_agent_id,))
                            if cur.fetchone():
                                cur.execute("SELECT community_id FROM posts WHERE id = ?", (post_id,))
                                post_row = cur.fetchone()
                                if post_row:
                                    ENGINE.schedule(random.randint(45, 120), EventPriority.HIGH, 'AGENT_REPLY', {
                                        'community_id': post_row['community_id'],
                                        'agent_id': parent_agent_id,
                                        'parent_comment_text': content,
                                        'post_id': post_id,
                                        'reply_to_comment_id': comment_id
                                    })
                    finally:
                        conn.close()

                    self.respond_json({'success': True, 'comment_id': comment_id})
                else:
                    self.respond_json({'error': 'Invalid comment path'}, status=400)
            else:
                self.respond_json({'error': 'Unknown API path'}, status=404)
        except OllamaError as e:
            try:
                self.respond_json({'error': str(e)}, status=500)
            except Exception:
                pass
        except Exception as e:
            try:
                self.respond_json({'error': f"Internal server error: {e}"}, status=500)
            except Exception:
                pass

    def serve_static(self, path: str) -> None:
        # Map '/' to index.html
        if path == '/' or path == '':
            filename = 'index.html'
        else:
            filename = path.lstrip('/')
        # Prevent directory traversal
        filename = os.path.normpath(filename)
        full_path = os.path.join(STATIC_DIR, filename)
        if not full_path.startswith(STATIC_DIR):
            self.send_error(HTTPStatus.FORBIDDEN, "Forbidden")
            return
        if os.path.isdir(full_path):
            full_path = os.path.join(full_path, 'index.html')
        if os.path.isfile(full_path):
            try:
                with open(full_path, 'rb') as f:
                    content = f.read()
                # Determine content type
                if full_path.endswith('.html'):
                    ctype = 'text/html'
                elif full_path.endswith('.js'):
                    ctype = 'application/javascript'
                elif full_path.endswith('.css'):
                    ctype = 'text/css'
                elif full_path.endswith('.json'):
                    ctype = 'application/json'
                elif full_path.endswith('.png'):
                    ctype = 'image/png'
                elif full_path.endswith('.jpg') or full_path.endswith('.jpeg'):
                    ctype = 'image/jpeg'
                else:
                    ctype = 'application/octet-stream'
                self.send_response(HTTPStatus.OK)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except Exception as e:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(e))
        else:
            # Fallback: return index.html for client-side routing
            index_path = os.path.join(STATIC_DIR, 'index.html')
            if os.path.isfile(index_path):
                with open(index_path, 'rb') as f:
                    content = f.read()
                self.send_response(HTTPStatus.OK)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "File not found")


class SocialHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address) -> None:
        exc_type, exc, _ = sys.exc_info()
        if isinstance(exc, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def run_server(host: str = 'localhost', port: int = 8080) -> None:
    init_db()
    httpd = SocialHTTPServer((host, port), RequestHandler)
    print(f"Serving on http://{host}:{port}")
    try:
        # Reset any stuck processing events back to pending
        conn = get_db_connection()
        try:
            conn.execute("UPDATE simulation_events SET status = 'pending', claimed_at = NULL WHERE status = 'processing'")
            conn.commit()
        finally:
            conn.close()

        # Start the central simulation engine
        ENGINE.start()
        
        # Auto-boot all communities on startup by scheduling their heartbeats.
        # We use dedupe_key + replace_existing=False so that if a community
        # already has a pending event, we don't duplicate it or override its timestamp.
        # Our updated schedule() logic natively handles replacing terminal rows, so no deletes are needed.
        conn = get_db_connection()
        try:
            # Mark all as active for the UI
            conn.execute("UPDATE communities SET active = 1")
            conn.commit()
            
            cur = conn.cursor()
            cur.execute("SELECT * FROM communities")
            start_delay = 5
            for row in cur.fetchall():
                comm_id = row['id']
                sim = Simulation(
                    comm_id,
                    row['name'],
                    row['description'],
                    row['model'],
                    row['posting_rate'],
                    row['tone'],
                    row['style_notes'],
                    row['mood'],
                    row['conflict_level'],
                    row['energy'],
                    row['trendiness'],
                    row['novelty_pressure'],
                    row['current_topics']
                )
                SIMULATIONS[comm_id] = sim
                ENGINE.schedule(
                    start_delay,
                    EventPriority.LOW,
                    'COMMUNITY_POST',
                    {'community_id': comm_id},
                    dedupe_key=f"COMMUNITY_POST_{comm_id}",
                    replace_existing=False
                )
                ENGINE.schedule(
                    start_delay + 60,
                    EventPriority.LOW,
                    'COMMUNITY_DRIFT',
                    {'community_id': comm_id},
                    dedupe_key=f"COMMUNITY_DRIFT_{comm_id}",
                    replace_existing=False
                )
                start_delay += 2 # stagger start times
        finally:
            conn.close()
        
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # Stop simulation engine on shutdown
        ENGINE.stop()
        httpd.server_close()


if __name__ == '__main__':
    run_server(host='0.0.0.0', port=5000)
