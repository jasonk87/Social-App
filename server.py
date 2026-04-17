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
import sqlite3
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, unquote
from typing import Dict, List, Optional, Any

import requests

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
        # Ensure Human user exists
        cur.execute("SELECT id FROM agents WHERE username = 'You' AND model = 'none'")
        if not cur.fetchone():
            cur.execute("INSERT INTO agents (username, model, persona) VALUES ('You', 'none', 'The human user')")
            
        conn.commit()
    finally:
        conn.close()


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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
personalities you generate! Make this specific agent highly technical, overly emotional,
conspiratorial, tutorial-focused, a storyteller, OR someone who only asks questions. Pick ONE extreme trait.

Respond with ONLY ONE JSON object with the following keys:
  "username": a concise, imaginative alias (no spaces, no punctuation other
               than underscores). It should feel like an internet handle.
  "persona": a detailed, 2-sentence description of the character's specific quirks,
             unique communication style, and extreme perspectives. Avoid mentioning it is an AI.
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


def generate_post(model: str, persona: str, community_name: str, description: str, tone: str, style_notes: str = "") -> Dict[str, str]:
    """Generate a post title and content for a community.

    Args:
        model: Name of the model to use.
        persona: Persona description of the posting agent.
        community_name: Name of the community.
        description: Description or theme of the community.

    Returns:
        A dictionary with 'title' and 'content'.
    """
    prompt = f"""You are writing a forum post in a community called '{community_name}'.
Community description: {description}
Community tone guidance: {tone_guidance(tone, style_notes)}
Your persona: {persona}

It is CRITICAL that your post strongly matches your persona and communication style.
Make it feel like a real person posting online, not an essay or whitepaper.
Aggressively vary the formatting and structure. Some posts should be short. Some should be anecdotal. Some should ask questions.
If you are a storyteller, share a vivid anecdote. If you are a tutorial-maker, share a step-by-step tip. If you are a debater, challenge a common assumption.
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


def generate_comment(model: str, persona: str, community_name: str, post_title: str, post_content: str, tone: str, style_notes: str = "") -> str:
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
    prompt = f"""You are replying to a post in the community '{community_name}'.
Community tone guidance: {tone_guidance(tone, style_notes)}
Your persona: {persona}

Post title: {post_title}
Post content: {post_content}

Write a short comment (1-3 sentences, occasionally 4 if needed) heavily adopting your persona. Sound like a real participant.
Disagree, agree, tease, ask a follow-up, or share a bizarre tangent if your persona dictates it. Keep it conversational and specific.
Do not sound like a lecturer, therapist, consultant, or academic unless the community tone explicitly demands it.
Avoid mentioning that you are an AI or referencing the instructions. Do not output
JSON, just the comment text.
"""
    comment = generate_text(model, prompt)
    return comment.strip()


def generate_comment_reply(model: str, persona: str, community_name: str, parent_comment: str, tone: str, style_notes: str = "") -> str:
    """Generate a comment in reply to another comment.

    Args:
        model: Name of the model to use.
        persona: Persona description of the commenting agent.
        community_name: Name of the community.
        parent_comment: Content of the comment being replied to.

    Returns:
        A string containing the comment.
    """
    prompt = f"""You are replying to a comment in the community '{community_name}'.
Community tone guidance: {tone_guidance(tone, style_notes)}
Your persona: {persona}

Previous comment: {parent_comment}

Write a short reply (1-3 sentences, occasionally 4 if needed) to the previous comment heavily adopting your persona.
Debate them, build off their idea, crack a joke, ask a question, or provide a counterpoint. Make it feel like an actual back-and-forth.
Avoid bloated wording and avoid turning this into a mini-essay unless the community tone explicitly calls for that.
Avoid mentioning that you are an AI or referencing the instructions. Do not output
JSON, just the reply text.
"""
    reply = generate_text(model, prompt)
    return reply.strip()


# -----------------------------------------------------------------------------
# Simulation engine
#
# Each community can run its own simulation in a separate thread.  When a
# simulation is active, the engine periodically selects an agent and
# generates either a new post or comments on an existing post.  The
# simulation uses the community's configured model and posting rate.  If
# an error occurs talking to Ollama, the simulation terminates so that it
# does not spin indefinitely.
#

class Simulation:
    def __init__(self, community_id: int, name: str, description: str, model: str, posting_rate: int, tone: str = DEFAULT_COMMUNITY_TONE, style_notes: str = ""):
        self.community_id = community_id
        self.name = name
        self.description = description or name
        self.model = model
        self.posting_rate = max(30, posting_rate)  # minimum 30 seconds between actions
        self.tone = normalize_tone(tone)
        self.style_notes = style_notes or ""
        self._stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def effective_posting_rate(self) -> int:
        profile = tone_runtime_profile(self.tone)
        return max(30, int(round(self.posting_rate * profile["cadence_multiplier"])))

    def start(self) -> None:
        if not self.thread.is_alive():
            self._stop_event.clear()
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=1)

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.step()
            except Exception as e:
                # Print error and continue simulation so it doesn't permanently crash
                print(f"Simulation error in community '{self.name}': {e}")
            # Sleep until next posting cycle or early exit
            for _ in range(self.effective_posting_rate()):
                if self._stop_event.is_set():
                    return
                time.sleep(1)

    def step(self) -> None:
        """Perform a single simulation step: either create a new post or comment."""
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            # Fetch all agents for this community
            cur.execute(
                """
                SELECT agents.id, agents.username, agents.persona, agents.model
                FROM agents
                JOIN community_agents ON agents.id = community_agents.agent_id
                WHERE community_agents.community_id = ?
                """,
                (self.community_id,),
            )
            agents = cur.fetchall()
            if not agents:
                # If no agents yet, create a couple
                for _ in range(3):
                    persona = create_persona(self.model)
                    agent_id = add_agent(persona['username'], self.model, persona['persona'])
                    assign_agent_to_community(agent_id, self.community_id)
                cur.execute(
                    """
                    SELECT agents.id, agents.username, agents.persona, agents.model
                    FROM agents
                    JOIN community_agents ON agents.id = community_agents.agent_id
                    WHERE community_agents.community_id = ?
                    """,
                    (self.community_id,),
                )
                agents = cur.fetchall()
            # With probability favouring new posts when there are fewer posts
            cur.execute("SELECT COUNT(*) FROM posts WHERE community_id = ?", (self.community_id,))
            post_count = cur.fetchone()[0]
            profile = tone_runtime_profile(self.tone)
            # Determine action
            make_new_post = post_count < 3 or random.random() < profile["new_post_bias"]
            # 10% chance to introduce a new agent if the population is under 20
            if len(agents) < 20 and random.random() < 0.10:
                new_persona = create_persona(self.model)
                new_agent_id = add_agent(new_persona['username'], self.model, new_persona['persona'])
                assign_agent_to_community(new_agent_id, self.community_id)
                cur.execute("SELECT id, username, persona, model FROM agents WHERE id = ?", (new_agent_id,))
                agent_row = cur.fetchone()
                print(f"[{self.name}] A new agent joined the community: {agent_row['username']}")
            else:
                agent_row = random.choice(agents)
                
            agent_id = agent_row['id']
            persona = agent_row['persona']
            model = agent_row['model']
            if make_new_post:
                # Generate post
                post_data = generate_post(model, persona, self.name, self.description, self.tone, self.style_notes)
                post_id = add_post(self.community_id, agent_id, post_data['title'], post_data['content'])
                print(f"[{self.name}] Generated new post: {post_data['title']}")
            else:
                # Comment on existing post or reply to comment
                reply_to_comment = False
                cur.execute("SELECT COUNT(*) FROM comments JOIN posts ON comments.post_id = posts.id WHERE posts.community_id = ?", (self.community_id,))
                comment_count = cur.fetchone()[0]
                if comment_count > 0 and random.random() < profile["reply_bias"]:
                    reply_to_comment = True

                if reply_to_comment:
                    cur.execute(
                        """
                        SELECT comments.id, comments.content, comments.post_id
                        FROM comments
                        JOIN posts ON comments.post_id = posts.id
                        JOIN agents ON comments.agent_id = agents.id
                        WHERE posts.community_id = ?
                        ORDER BY CASE WHEN agents.username = 'You' THEN 0 ELSE 1 END, RANDOM()
                        LIMIT 1
                        """,
                        (self.community_id,),
                    )
                    row = cur.fetchone()
                    if row:
                        parent_id = row['id']
                        parent_content = row['content']
                        post_id = row['post_id']
                        comment_text = generate_comment_reply(model, persona, self.name, parent_content, self.tone, self.style_notes)
                        add_comment(post_id, agent_id, parent_id, comment_text)
                        print(f"[{self.name}] Added comment reply by {agent_row['username']}")
                else:
                    # Select a random existing post
                    cur.execute(
                        """
                        SELECT posts.id, posts.title, posts.content
                        FROM posts
                        JOIN agents ON posts.agent_id = agents.id
                        WHERE posts.community_id = ?
                        ORDER BY CASE WHEN agents.username = 'You' THEN 0 ELSE 1 END, RANDOM()
                        LIMIT 1
                        """,
                        (self.community_id,),
                    )
                    row = cur.fetchone()
                    if row:
                        post_id = row['id']
                        title = row['title']
                        content = row['content']
                        comment_text = generate_comment(model, persona, self.name, title, content, self.tone, self.style_notes)
                        add_comment(post_id, agent_id, None, comment_text)
                        print(f"[{self.name}] Added comment by {agent_row['username']}")
        finally:
            conn.close()


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
        return community_id
    finally:
        conn.close()


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
        conn.commit()
        return comment_id
    finally:
        conn.close()


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
            comments_tree = []
            id_to_node = {}
            # Build tree structure
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

    def respond_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_api_get(self, parsed) -> None:
        path = parsed.path[len('/api/'):]  # strip '/api/'
        query = parse_qs(parsed.query)
        try:
            if path == 'communities':
                communities = []
                conn = get_db_connection()
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT * FROM communities ORDER BY name")
                    for row in cur.fetchall():
                        communities.append({
                            'id': row['id'],
                            'name': row['name'],
                            'description': row['description'],
                            'model': row['model'],
                            'posting_rate': row['posting_rate'],
                            'active': bool(row['active']),
                            'tone': normalize_tone(row['tone']),
                            'style_notes': row['style_notes'] or '',
                        })
                finally:
                    conn.close()
                self.respond_json({'communities': communities})
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
                    self.respond_json({'posts': posts})
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
                        cur.execute("SELECT id, title, content, created_at, community_id FROM posts WHERE agent_id = ? ORDER BY created_at DESC LIMIT 20", (agent_id,))
                        posts = [dict(r) for r in cur.fetchall()]
                        
                        # Fetch recent comments
                        cur.execute("SELECT id, content, created_at, post_id, parent_id FROM comments WHERE agent_id = ? ORDER BY created_at DESC LIMIT 20", (agent_id,))
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
            if path == 'communities':
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
                # Note: Initial agents are intentionally NOT generated here to avoid blocking properties.
                # The simulation engine's step() method will automatically generate them when started.
                # Immediately spin up the new background simulation thread
                sim = Simulation(community_id, name, description, model, posting_rate, tone, style_notes)
                SIMULATIONS[community_id] = sim
                sim.start()
                
                self.respond_json({'success': True, 'community_id': community_id})
            elif path.startswith('community/') and path.endswith('/update'):
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
                        sim.stop()
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
            elif path.startswith('community/') and path.endswith('/post'):
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
                    conn = get_db_connection()
                    try:
                        cur = conn.cursor()
                        cur.execute("SELECT id FROM agents WHERE username = 'You' AND model = 'none'")
                        human_row = cur.fetchone()
                        post_id = add_post(row['id'], human_row['id'], title, content)
                    finally:
                        conn.close()
                    self.respond_json({'success': True, 'post_id': post_id})
                else:
                    self.respond_json({'error': 'Invalid post path'}, status=400)
            elif path.startswith('post/') and path.endswith('/comment'):
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
                    conn = get_db_connection()
                    try:
                        cur = conn.cursor()
                        cur.execute("SELECT id FROM agents WHERE username = 'You' AND model = 'none'")
                        human_row = cur.fetchone()
                        comment_id = add_comment(post_id, human_row['id'], parent_id, content)
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
        # Auto-boot all communities on startup
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM communities")
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
                sim.start()
        finally:
            conn.close()
        
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # Stop all running simulations on shutdown
        for sim in list(SIMULATIONS.values()):
            sim.stop()
        httpd.server_close()


if __name__ == '__main__':
    run_server(host='0.0.0.0', port=5000)
