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

By default the server listens on localhost port 8080.  Open a browser
and navigate to http://localhost:8080/ to access the UI.
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
        community_columns = {row[1] for row in cur.execute("PRAGMA table_info(communities)").fetchall()}
        if "tone" not in community_columns:
            cur.execute("ALTER TABLE communities ADD COLUMN tone TEXT DEFAULT 'casual'")
        if "style_notes" not in community_columns:
            cur.execute("ALTER TABLE communities ADD COLUMN style_notes TEXT DEFAULT ''")
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
                memory TEXT
            )
            """
        )
        agent_columns = {row[1] for row in cur.execute("PRAGMA table_info(agents)").fetchall()}
        if "memory" not in agent_columns:
            cur.execute("ALTER TABLE agents ADD COLUMN memory TEXT")
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
                event_data TEXT NOT NULL
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sim_events_time ON simulation_events (timestamp, priority)")

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

PERSONA_PROMPT = """
You are generating a SINGLE unique username and persona description for an AI agent
participating in an online community. It is CRITICAL that you wildly vary the
personalities you generate! Make this specific agent a casual fan, a hardcore enthusiast,
a newcomer asking for help, a salty veteran, a helpful guide, a meme poster, OR a lore master. Pick ONE trait.
Focus on making them feel like a real Reddit user who is passionate about the community's topic.
Do NOT make them sound like a computer program, a software engineer debugging a simulation, or overly academic.

Respond with ONLY ONE JSON object with the following keys:
  "username": a concise, imaginative alias (no spaces, no punctuation other
               than underscores). It should feel like an internet handle.
  "persona": a detailed, 2-sentence description of the character's specific quirks,
             unique communication style, and perspectives. Avoid mentioning it is an AI.
Return only the JSON object and no other commentary. Do not return a list.
"""


def create_persona(model: str) -> Dict[str, str]:
    """Generate a new AI persona using the specified model.

    Returns a dictionary with 'username' and 'persona'.
    """
    response = generate_text(model, PERSONA_PROMPT)
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


def generate_post(model: str, persona: str, community_name: str, description: str, tone: str, style_notes: str = "", memory: str = "") -> Dict[str, str]:
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
    prompt = f"""You are writing a forum post in a community called '{community_name}'.
Community description: {description}
Community tone guidance: {tone_guidance(tone, style_notes)}
Your persona: {persona}{memory_section}

It is CRITICAL that your post strongly matches your persona and communication style.
Make it feel like a real person posting on Reddit, focused heavily on the actual subject matter of the community.
Discuss gameplay, share tips, talk about features, lore, or ask relevant questions based on the community description.
Do NOT talk about buffer overflows, non-Euclidean geometry, simulations, memory allocation, algorithms, latency, bugs in reality, or any computer science jargon unless the community is specifically about computer programming.
Aggressively vary the formatting and structure. Some posts should be short. Some should be anecdotal. Some should ask questions.
If you are a storyteller, share a vivid anecdote. If you are a helpful guide, share a step-by-step tip. If you are a debater, challenge a common assumption.
Avoid generic observations, inflated vocabulary, and long academic padding.

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


def generate_comment(model: str, persona: str, community_name: str, post_title: str, post_content: str, tone: str, style_notes: str = "", memory: str = "") -> str:
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
    prompt = f"""You are replying to a post in the community '{community_name}'.
Community tone guidance: {tone_guidance(tone, style_notes)}
Your persona: {persona}{memory_section}

Post title: {post_title}
Post content: {post_content}

Write a short comment (1-3 sentences, occasionally 4 if needed) heavily adopting your persona. Sound like a real Reddit user participating in the community.
Disagree, agree, tease, ask a follow-up, or share a bizarre tangent if your persona dictates it. Keep it conversational and specific to the community topic.
Do NOT talk about buffer overflows, non-Euclidean geometry, simulations, memory allocation, algorithms, latency, bugs in reality, or any computer science jargon unless the community is specifically about computer programming.
Do not sound like a lecturer, therapist, consultant, or academic unless the community tone explicitly demands it.
Avoid mentioning that you are an AI or referencing the instructions. Do not output
JSON, just the comment text.
"""
    comment = generate_text(model, prompt)
    return comment.strip()


def generate_comment_reply(model: str, persona: str, community_name: str, parent_comment: str, tone: str, style_notes: str = "", memory: str = "") -> str:
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
    prompt = f"""You are replying to a comment in the community '{community_name}'.
Community tone guidance: {tone_guidance(tone, style_notes)}
Your persona: {persona}{memory_section}

Previous comment: {parent_comment}

Write a short reply (1-3 sentences, occasionally 4 if needed) to the previous comment heavily adopting your persona.
Debate them, build off their idea, crack a joke, ask a question, or provide a counterpoint. Make it feel like an actual Reddit back-and-forth.
Do NOT talk about buffer overflows, non-Euclidean geometry, simulations, memory allocation, algorithms, latency, bugs in reality, or any computer science jargon unless the community is specifically about computer programming.
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

    def schedule(self, delay: float, priority: EventPriority, event_type: str, data: dict):
        timestamp = time.time() + delay
        conn = get_db_connection()
        try:
            conn.execute(
                "INSERT INTO simulation_events (timestamp, priority, event_type, event_data) VALUES (?, ?, ?, ?)",
                (timestamp, priority.value, event_type, json.dumps(data))
            )
            conn.commit()
        finally:
            conn.close()

    def _run(self):
        while not self._stop_event.is_set():
            event = None
            conn = get_db_connection()
            try:
                # Find the oldest event that is due, ordering by timestamp and then priority (lowest value = highest priority)
                cur = conn.cursor()
                cur.execute(
                    "SELECT id, event_type, event_data FROM simulation_events WHERE timestamp <= ? ORDER BY timestamp ASC, priority ASC LIMIT 1",
                    (time.time(),)
                )
                row = cur.fetchone()
                if row:
                    # Attempt to delete the event to "claim" it. In a multi-process environment we might need a more robust transaction,
                    # but here the single daemon thread logic applies.
                    cur.execute("DELETE FROM simulation_events WHERE id = ?", (row['id'],))
                    if cur.rowcount > 0:
                        conn.commit()
                        event = SimEvent(0, 0, row['event_type'], json.loads(row['event_data']))
                    else:
                        conn.rollback()
            except Exception as e:
                print(f"Simulation engine queue error: {e}")
            finally:
                conn.close()

            if event:
                try:
                    if event.event_type == 'COMMUNITY_POST':
                        self._do_community_post(event.data)
                    elif event.event_type == 'AGENT_REPLY':
                        self._do_agent_reply(event.data)
                except Exception as e:
                    print(f"Simulation engine error processing {event.event_type}: {e}")
            else:
                time.sleep(1)

    def _do_community_post(self, data: dict):
        community_id = data['community_id']
        sim = SIMULATIONS.get(community_id)
        if not sim:
            return

        # Schedule the next heartbeat
        self.schedule(sim.effective_posting_rate(), EventPriority.LOW, 'COMMUNITY_POST', {'community_id': community_id})

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
                persona = create_persona(sim.model)
                agent_id = add_agent(persona['username'], sim.model, persona['persona'])
                assign_agent_to_community(agent_id, community_id)
                agents.append({'id': agent_id, 'username': persona['username'], 'persona': persona['persona'], 'model': sim.model, 'memory': None})
                
        profile = tone_runtime_profile(sim.tone)
        # Determine action
        make_new_post = post_count < 3 or random.random() < profile["new_post_bias"]
        # 10% chance to introduce a new agent if the population is under 20
        if len(agents) < 20 and random.random() < 0.10:
            new_persona = create_persona(sim.model)
            new_agent_id = add_agent(new_persona['username'], sim.model, new_persona['persona'])
            assign_agent_to_community(new_agent_id, community_id)
            agent_row = {'id': new_agent_id, 'username': new_persona['username'], 'persona': new_persona['persona'], 'model': sim.model, 'memory': None}
            print(f"[{sim.name}] A new agent joined the community: {agent_row['username']}")
        else:
            agent_row = random.choice(agents)

        agent_id = agent_row['id']
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

        # Phase 3: Generate content
        if make_new_post:
            # Generate post
            post_data = generate_post(model, persona, sim.name, sim.description, sim.tone, sim.style_notes, memory_str)
            post_id = add_post(community_id, agent_id, post_data['title'], post_data['content'])
            print(f"[{sim.name}] Generated new post: {post_data['title']}")
            append_memory(f"Created a post titled '{post_data['title']}': {post_data['content']}")
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
                    comment_text = generate_comment_reply(model, persona, sim.name, parent_content, sim.tone, sim.style_notes, memory_str)
                    new_comment_id = add_comment(post_id, agent_id, parent_id, comment_text)
                    print(f"[{sim.name}] Added comment reply by {agent_row['username']}")
                    append_memory(f"Replied to a comment '{parent_content}' with: {comment_text}")

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
                    comment_text = generate_comment(model, persona, sim.name, title, content, sim.tone, sim.style_notes, memory_str)
                    new_comment_id = add_comment(post_id, agent_id, None, comment_text)
                    print(f"[{sim.name}] Added comment by {agent_row['username']}")
                    append_memory(f"Commented on post '{title}' with: {comment_text}")

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
        try:
            cur = conn.cursor()
            cur.execute("SELECT username, persona, model, memory FROM agents WHERE id = ?", (agent_id,))
            agent_row = cur.fetchone()
            if not agent_row:
                return
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

        comment_text = generate_comment_reply(model, persona, sim.name, parent_comment_text, sim.tone, sim.style_notes, memory_str)
        new_comment_id = add_comment(post_id, agent_id, reply_to_comment_id, comment_text)
        print(f"[{sim.name}] Added priority comment reply by {agent_row['username']}")
        append_memory(f"Replied to a comment '{parent_comment_text}' with: {comment_text}")

        # Determine if the comment we replied to was by an AI, if so, schedule another reply
        if reply_to_comment_id:
            conn = get_db_connection()
            try:
                cur = conn.cursor()
                cur.execute("SELECT agent_id FROM comments WHERE id = ?", (reply_to_comment_id,))
                c_row = cur.fetchone()
                if c_row:
                    parent_agent_id = c_row['agent_id']
                    cur.execute("SELECT id FROM agents WHERE id = ? AND model != 'none'", (parent_agent_id,))
                    pa_row = cur.fetchone()
                    if pa_row:
                        # 70% chance to continue the chain
                        if random.random() < 0.7:
                            ENGINE.schedule(random.randint(60, 180), EventPriority.HIGH, 'AGENT_REPLY', {
                                'community_id': community_id,
                                'agent_id': parent_agent_id,
                                'reply_to_comment_id': new_comment_id,
                                'parent_comment_text': comment_text,
                                'post_id': post_id
                            })
            finally:
                conn.close()

ENGINE = SimulationEngine()

class Simulation:
    def __init__(self, community_id: int, name: str, description: str, model: str, posting_rate: int, tone: str = DEFAULT_COMMUNITY_TONE, style_notes: str = ""):
        self.community_id = community_id
        self.name = name
        self.description = description or name
        self.model = model
        self.posting_rate = max(30, posting_rate)  # minimum 30 seconds between actions
        self.tone = normalize_tone(tone)
        self.style_notes = style_notes or ""

    def effective_posting_rate(self) -> int:
        profile = tone_runtime_profile(self.tone)
        return max(30, int(round(self.posting_rate * profile["cadence_multiplier"])))


# Registry of active simulations keyed by community ID
SIMULATIONS: Dict[int, Simulation] = {}


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


def fetch_community_feed(community_id: int) -> List[Dict[str, Any]]:
    """Return a list of posts with nested comments for a community."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT posts.id as post_id, posts.title, posts.content, posts.created_at,
                   agents.username AS author, agents.id AS agent_id
            FROM posts
            JOIN agents ON posts.agent_id = agents.id
            WHERE posts.community_id = ?
            ORDER BY posts.created_at DESC
            LIMIT 50
            """,
            (community_id,),
        )
        posts = []
        post_rows = cur.fetchall()
        for p_row in post_rows:
            post_id = p_row['post_id']
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
            posts.append({
                'id': post_id,
                'title': p_row['title'],
                'content': p_row['content'],
                'author': p_row['author'],
                'agent_id': p_row['agent_id'],
                'created_at': p_row['created_at'],
                'comments': comments_tree,
            })
        return posts
    finally:
        conn.close()


def fetch_home_feed(user_id: int, sort: str = "latest") -> List[Dict[str, Any]]:
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
        return posts[:120]
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

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
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
                posts = fetch_home_feed(current_user['id'], sort)
                self.respond_json({
                    'sort': sort,
                    'posts': posts,
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
                    posts = fetch_community_feed(row['id'])
                    self.respond_json({
                        'posts': posts,
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
                ENGINE.schedule(5, EventPriority.LOW, 'COMMUNITY_POST', {'community_id': community_id})
                
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


def run_server(host: str = 'localhost', port: int = 8080) -> None:
    init_db()
    httpd = ThreadingHTTPServer((host, port), RequestHandler)
    print(f"Serving on http://{host}:{port}")
    try:
        # Clear any pending COMMUNITY_POST events to avoid overlapping timers across restarts
        conn = get_db_connection()
        try:
            conn.execute("DELETE FROM simulation_events WHERE event_type = 'COMMUNITY_POST'")
            conn.commit()
        finally:
            conn.close()

        # Start the central simulation engine
        ENGINE.start()
        
        # Auto-boot all communities on startup by scheduling their heartbeats
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
                )
                SIMULATIONS[comm_id] = sim
                ENGINE.schedule(start_delay, EventPriority.LOW, 'COMMUNITY_POST', {'community_id': comm_id})
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
    run_server(host='0.0.0.0', port=3000)
