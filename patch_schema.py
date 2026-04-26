import re

with open("server.py", "r") as f:
    content = f.read()

# 1. Update init_db
init_db_search = """        # Comments table"""
init_db_replace = """        # Add locked column to posts if missing
        post_columns = {row[1] for row in cur.execute("PRAGMA table_info(posts)").fetchall()}
        if "locked" not in post_columns:
            cur.execute("ALTER TABLE posts ADD COLUMN locked INTEGER DEFAULT 0")

        # Comments table"""

content = content.replace(init_db_search, init_db_replace)


# 2. Update fetch_home_feed
home_feed_search = """            SELECT
                posts.id AS post_id,
                posts.title,
                posts.content,
                posts.created_at,
                posts.community_id,"""

home_feed_replace = """            SELECT
                posts.id AS post_id,
                posts.title,
                posts.content,
                posts.created_at,
                posts.community_id,
                posts.locked,"""

content = content.replace(home_feed_search, home_feed_replace)

home_feed_append_search = """            posts.append({
                "id": row["post_id"],
                "title": row["title"],
                "content": row["content"],
                "created_at": row["created_at"],
                "community_id": row["community_id"],"""

home_feed_append_replace = """            posts.append({
                "id": row["post_id"],
                "title": row["title"],
                "content": row["content"],
                "created_at": row["created_at"],
                "community_id": row["community_id"],
                "locked": bool(row["locked"]),"""

content = content.replace(home_feed_append_search, home_feed_append_replace)


# 3. Update fetch_community_feed
comm_feed_search = """        cur.execute(
            \"\"\"
            SELECT posts.id as post_id, posts.title, posts.content, posts.created_at,
                   agents.username AS author, agents.id AS agent_id
            FROM posts"""

comm_feed_replace = """        cur.execute(
            \"\"\"
            SELECT posts.id as post_id, posts.title, posts.content, posts.created_at, posts.locked,
                   agents.username AS author, agents.id AS agent_id
            FROM posts"""

content = content.replace(comm_feed_search, comm_feed_replace)

comm_feed_target_search = """        if target_post_id is not None and target_post_id not in post_map:
            cur.execute(
                \"\"\"
                SELECT posts.id as post_id, posts.title, posts.content, posts.created_at,
                       agents.username AS author, agents.id AS agent_id
                FROM posts"""

comm_feed_target_replace = """        if target_post_id is not None and target_post_id not in post_map:
            cur.execute(
                \"\"\"
                SELECT posts.id as post_id, posts.title, posts.content, posts.created_at, posts.locked,
                       agents.username AS author, agents.id AS agent_id
                FROM posts"""

content = content.replace(comm_feed_target_search, comm_feed_target_replace)

comm_feed_append_search = """                post_map[post_id] = {
                    'id': post_id,
                    'title': p_row['title'],
                    'content': p_row['content'],
                    'author': p_row['author'],
                    'agent_id': p_row['agent_id'],
                    'created_at': p_row['created_at'],"""

comm_feed_append_replace = """                post_map[post_id] = {
                    'id': post_id,
                    'title': p_row['title'],
                    'content': p_row['content'],
                    'author': p_row['author'],
                    'agent_id': p_row['agent_id'],
                    'created_at': p_row['created_at'],
                    'locked': bool(p_row['locked']),"""

content = content.replace(comm_feed_append_search, comm_feed_append_replace)

with open("server.py", "w") as f:
    f.write(content)
