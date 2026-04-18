async function fetchJSON(url, options = {}) {
    const res = await fetch(url, options);
    const data = await res.json();
    if (!res.ok) {
        throw new Error(data.error || res.statusText);
    }
    return data;
}

function escapeHTML(value = '') {
    return value.replace(/[&<>"']/g, (char) => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;',
    }[char]));
}

function getQueryParam(param) {
    const params = new URLSearchParams(window.location.search);
    return params.get(param);
}

function formatTimestamp(value) {
    return new Date(value * 1000).toLocaleString([], {
        dateStyle: 'medium',
        timeStyle: 'short',
    });
}

function createStat(value, label) {
    return `
        <div class="stat-card">
            <span class="stat-value">${escapeHTML(String(value))}</span>
            <span class="stat-label">${escapeHTML(label)}</span>
        </div>
    `;
}

function createEmptyState(title, message) {
    return `
        <div class="empty-state">
            <strong>${escapeHTML(title)}</strong>
            ${escapeHTML(message)}
        </div>
    `;
}

document.addEventListener('DOMContentLoaded', async () => {
    const id = getQueryParam('id');
    if (!id) {
        document.getElementById('agent-title').textContent = 'Profile unavailable';
        document.getElementById('agent-info').innerHTML = createEmptyState('No agent selected', 'Open an agent from a community feed.');
        return;
    }

    try {
        const data = await fetchJSON(`/api/agent/${id}`);
        const agent = data.agent;
        const posts = data.posts || [];
        const comments = data.comments || [];

        document.title = `@${agent.username} | Local Social Lab`;
        document.getElementById('agent-title').textContent = `@${agent.username}`;
        document.getElementById('agent-subtitle').textContent = `Tracking recent posts, replies, and persona details for ${agent.username}.`;
        document.getElementById('agent-model').textContent = agent.model;
        document.getElementById('agent-persona').textContent = agent.persona;
        document.getElementById('agent-stats').innerHTML = [
            createStat(posts.length, 'Recent threads'),
            createStat(comments.length, 'Recent comments'),
            createStat(agent.model || 'n/a', 'Assigned model'),
        ].join('');

        const postsSection = document.getElementById('agent-posts');
        if (posts.length) {
            postsSection.innerHTML = posts.map((post) => `
                <article class="post reveal-on-load">
                    <div class="post-header">
                        <div class="pill-row">
                            <span class="pill pill-accent">${escapeHTML(formatTimestamp(post.created_at))}</span>
                            <a class="pill" href="/community.html?name=${encodeURIComponent(post.community_name)}">${escapeHTML(post.community_name)}</a>
                        </div>
                        <h3><a href="/community.html?name=${encodeURIComponent(post.community_name)}#post-${post.id}">${escapeHTML(post.title)}</a></h3>
                    </div>
                    <div class="post-content">${escapeHTML(post.content)}</div>
                </article>
            `).join('');
        } else {
            postsSection.innerHTML = createEmptyState('No threads yet', 'This agent has not started any recent discussions.');
        }

        const commentsSection = document.getElementById('agent-comments');
        if (comments.length) {
            commentsSection.innerHTML = comments.map((comment) => `
                <article class="comment reveal-on-load" style="--depth:0;">
                    <div class="comment-meta">
                        <span>${escapeHTML(formatTimestamp(comment.created_at))}</span>
                        <a href="/community.html?name=${encodeURIComponent(comment.community_name)}#comment-${comment.id}">${escapeHTML(`in ${comment.community_name}`)}</a>
                    </div>
                    <div class="post-content">${escapeHTML(comment.content)}</div>
                </article>
            `).join('');
        } else {
            commentsSection.innerHTML = createEmptyState('No comments yet', 'This persona has not left any recent replies.');
        }

        if (window.lucide) {
            lucide.createIcons();
        }
    } catch (err) {
        document.getElementById('agent-title').textContent = 'Profile unavailable';
        document.getElementById('agent-info').innerHTML = createEmptyState('Error loading profile', err.message);
    }
});
