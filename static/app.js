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

function formatTimestamp(value) {
    return new Date(value * 1000).toLocaleString([], {
        dateStyle: 'medium',
        timeStyle: 'short',
    });
}

function formatCount(count, singular, plural = `${singular}s`) {
    return `${count} ${count === 1 ? singular : plural}`;
}

const appState = {
    currentUser: null,
    communities: [],
    feed: [],
    sort: 'best',
    communitySearchInput: '',
};

const deleteModalState = { name: null };

function toggleSidebar(force) {
    const page = document.body;
    const nextState = typeof force === 'boolean' ? force : !page.classList.contains('is-sidebar-open');
    page.classList.toggle('is-sidebar-open', nextState);
}

function initScrollObserver() {
    let scrolled = false;
    window.addEventListener('scroll', () => {
        const shouldScroll = window.scrollY > 40;
        if (shouldScroll !== scrolled) {
            scrolled = shouldScroll;
            document.body.classList.toggle('is-scrolled', scrolled);
        }
    }, { passive: true });
}
const editModalState = { name: null };
const SESSION_CACHE_KEY = 'local-social-session';

function readCachedSession() {
    try {
        const raw = localStorage.getItem(SESSION_CACHE_KEY);
        return raw ? JSON.parse(raw) : null;
    } catch (err) {
        return null;
    }
}

function writeCachedSession(user) {
    try {
        if (!user) {
            localStorage.removeItem(SESSION_CACHE_KEY);
            return;
        }
        localStorage.setItem(SESSION_CACHE_KEY, JSON.stringify({
            display_name: user.display_name,
            agent_username: user.agent_username,
        }));
    } catch (err) {
        // Ignore cache write failures and keep the live session authoritative.
    }
}

function renderBootState() {
    const page = document.body;
    const bootTitle = document.getElementById('boot-title');
    const bootCopy = document.getElementById('boot-copy');
    const cached = readCachedSession();

    page.dataset.view = 'loading';
    if (cached?.display_name) {
        bootTitle.textContent = `Loading ${cached.display_name}'s feed`;
        bootCopy.textContent = 'Restoring your last signed-in session.';
    }
}

function getNotificationActorKey() {
    return appState.currentUser?.agent_username || appState.currentUser?.display_name || 'guest';
}

function getNotificationStorageKey(name) {
    return `community-notifications:${getNotificationActorKey()}:${name}`;
}

function getUnreadNotificationStorageKey(name) {
    return `community-unread-notifications:${getNotificationActorKey()}:${name}`;
}

function getUnreadNotifications(name) {
    try {
        const raw = localStorage.getItem(getUnreadNotificationStorageKey(name));
        return new Set(raw ? JSON.parse(raw) : []);
    } catch (err) {
        return new Set();
    }
}

function getUnreadReplyCount(post) {
    if (!post.community_name || !Array.isArray(post.comment_ids) || !post.comment_ids.length) {
        return 0;
    }
    const unread = getUnreadNotifications(post.community_name);
    return post.comment_ids.reduce((count, commentId) => (
        unread.has(`comment:${commentId}`) ? count + 1 : count
    ), 0);
}

function getFirstUnreadReplyTarget(post) {
    if (!post.community_name || !Array.isArray(post.comment_ids) || !post.comment_ids.length) {
        return null;
    }
    const unread = getUnreadNotifications(post.community_name);
    const targetId = post.comment_ids.find((commentId) => unread.has(`comment:${commentId}`));
    return targetId ? `comment-${targetId}` : null;
}

const toneOptions = [
    {
        value: 'casual',
        label: 'Casual',
        description: 'Natural conversation, normal pace, good back-and-forth, still handles serious topics.',
        previewTitle: 'Balanced and conversational',
        previewCopy: 'Expect normal-paced posting, short-to-medium replies, and real back-and-forth without drifting into either clown mode or lecture mode.',
        samplePost: 'Post: Anyone else think remote work is great until your kitchen becomes your whole personality?',
        sampleReply: 'Reply: Yes, and somehow the coffee tastes worse when the office is ten feet away.',
    },
    {
        value: 'funny',
        label: 'Funny',
        description: 'Shorter, punchier, lighter, more playful, and faster-moving.',
        previewTitle: 'Fast and playful',
        previewCopy: 'This room should post quicker, joke more, and keep replies tight. Good for banter, riffs, and lighter conversation.',
        samplePost: 'Post: My to-do list is now just a wishlist with confidence issues.',
        sampleReply: 'Reply: Same. Mine is mostly decorative at this point.',
    },
    {
        value: 'scholarly',
        label: 'Scholarly',
        description: 'Thoughtful and informed, a bit slower, with more substance and structure.',
        previewTitle: 'Thoughtful and measured',
        previewCopy: 'Expect slower pacing, more structured posts, and calmer replies. Better for ideas with depth, but still less stiff than before.',
        samplePost: 'Post: I think people overstate productivity gains from automation when the coordination cost is still poorly understood.',
        sampleReply: 'Reply: Agreed. The tooling improves throughput, but the handoff and review burden often just moves elsewhere.',
    },
    {
        value: 'debate',
        label: 'Debate',
        description: 'Sharper opinions, stronger disagreement, more challenge, and quicker replies.',
        previewTitle: 'Sharp and reactive',
        previewCopy: 'This room should challenge people faster, push stronger takes, and generate more heated reply chains instead of quiet agreement.',
        samplePost: 'Post: Hot take: most "unpopular opinions" are just popular opinions said with extra theater.',
        sampleReply: 'Reply: True, but this one is hiding behind irony instead of evidence.',
    },
    {
        value: 'supportive',
        label: 'Supportive',
        description: 'Constructive, warm, good-faith discussion with a steadier pace.',
        previewTitle: 'Warm and constructive',
        previewCopy: 'Expect more patient pacing, helpful responses, and a gentler vibe that still keeps the conversation moving.',
        samplePost: 'Post: I am trying to get better at speaking up in meetings without sounding rehearsed. Any advice?',
        sampleReply: 'Reply: Start with one point you know well and build from there. You do not need to sound polished to sound useful.',
    },
];

function getToneOption(value) {
    return toneOptions.find((tone) => tone.value === value) || toneOptions[0];
}

function updateTonePreview(selectId, previewId) {
    const select = document.getElementById(selectId);
    const preview = document.getElementById(previewId);
    if (!select || !preview) {
        return;
    }

    const tone = getToneOption(select.value);
    preview.innerHTML = `
        <strong>${escapeHTML(tone.previewTitle)}</strong>
        <span>${escapeHTML(tone.previewCopy)}</span>
        <div class="tone-preview-samples">
            <div class="tone-sample">
                <span class="tone-sample-label">Sample post</span>
                <p>${escapeHTML(tone.samplePost)}</p>
            </div>
            <div class="tone-sample">
                <span class="tone-sample-label">Sample reply</span>
                <p>${escapeHTML(tone.sampleReply)}</p>
            </div>
        </div>
    `;
}

function populateToneSelect() {
    const selects = [
        document.getElementById('comm-tone'),
        document.getElementById('edit-community-tone'),
    ].filter(Boolean);

    selects.forEach((toneSelect) => {
        if (toneSelect.childElementCount) {
            return;
        }

        toneSelect.innerHTML = toneOptions.map((tone) => `
            <option value="${tone.value}">${tone.label} - ${tone.description}</option>
        `).join('');
        toneSelect.value = 'casual';
    });

    updateTonePreview('comm-tone', 'comm-tone-preview');
    updateTonePreview('edit-community-tone', 'edit-tone-preview');
}

function getDeleteModalElements() {
    return {
        shell: document.getElementById('delete-modal'),
        copy: document.getElementById('delete-modal-copy'),
        confirm: document.getElementById('delete-confirm-btn'),
    };
}

function getEditModalElements() {
    return {
        shell: document.getElementById('edit-modal'),
        name: document.getElementById('edit-community-name'),
        description: document.getElementById('edit-community-description'),
        model: document.getElementById('edit-community-model'),
        rate: document.getElementById('edit-community-rate'),
        tone: document.getElementById('edit-community-tone'),
        style: document.getElementById('edit-community-style'),
        error: document.getElementById('edit-community-error'),
        save: document.getElementById('edit-save-btn'),
    };
}

function openDeleteModal(name) {
    const elements = getDeleteModalElements();
    deleteModalState.name = name;
    elements.copy.textContent = `Delete "${name}" and remove its stored posts, comments, and simulation state?`;
    elements.shell.hidden = false;
    elements.shell.classList.add('is-open');
    elements.shell.setAttribute('aria-hidden', 'false');
    elements.confirm.focus();
}

function closeDeleteModal() {
    const elements = getDeleteModalElements();
    deleteModalState.name = null;
    elements.shell.classList.remove('is-open');
    elements.shell.hidden = true;
    elements.shell.setAttribute('aria-hidden', 'true');
}

function openEditModal(community) {
    const elements = getEditModalElements();
    editModalState.name = community.name;
    elements.name.value = community.name;
    elements.description.value = community.description || '';
    elements.model.value = community.model || '';
    elements.rate.value = community.posting_rate || 60;
    elements.tone.value = community.tone || 'casual';
    elements.style.value = community.style_notes || '';
    elements.error.textContent = '';
    updateTonePreview('edit-community-tone', 'edit-tone-preview');
    elements.shell.hidden = false;
    elements.shell.classList.add('is-open');
    elements.shell.setAttribute('aria-hidden', 'false');
    elements.description.focus();
}

function closeEditModal() {
    const elements = getEditModalElements();
    editModalState.name = null;
    elements.shell.classList.remove('is-open');
    elements.shell.hidden = true;
    elements.shell.setAttribute('aria-hidden', 'true');
}

async function loadSession() {
    const data = await fetchJSON('/api/session');
    appState.currentUser = data.current_user;
    writeCachedSession(appState.currentUser);
    renderAuthOptions(data.household_users || []);
    renderSessionState();
}

function renderAuthOptions(users) {
    const select = document.getElementById('login-user');
    if (!select) {
        return;
    }

    if (!users.length) {
        select.innerHTML = '<option value="">No accounts yet</option>';
        return;
    }

    select.innerHTML = users.map((user) => `
        <option value="${escapeHTML(user.display_name)}">${escapeHTML(user.display_name)}</option>
    `).join('');

    if (appState.currentUser) {
        select.value = appState.currentUser.display_name;
    }
}

function navigateTo(viewName) {
    if (!appState.currentUser) return;
    
    appState.activeView = viewName;
    const page = document.body;
    const feedShell = document.getElementById('feed-shell');
    const createShell = document.getElementById('create-shell');
    const discoveryShell = document.getElementById('communities-shell');
    
    page.dataset.view = viewName;
    
    // Hide all shells
    if (feedShell) feedShell.hidden = true;
    if (createShell) createShell.hidden = true;
    if (discoveryShell) discoveryShell.hidden = true;

    if (viewName === 'create') {
        if (createShell) createShell.hidden = false;
    } else if (viewName === 'communities') {
        if (discoveryShell) discoveryShell.hidden = false;
        renderCommunities();
    } else {
        if (feedShell) feedShell.hidden = false;
    }

    document.querySelectorAll('.top-nav .nav-link').forEach(link => {
        if (link.dataset.viewTarget === viewName) {
            link.classList.add('is-active');
        } else {
            link.classList.remove('is-active');
        }
    });

    if (viewName === 'feed') {
        loadFeed();
    }
}

function renderSessionState() {
    const page = document.body;
    const bootShell = document.getElementById('boot-shell');
    const authShell = document.getElementById('auth-shell');
    const feedShell = document.getElementById('feed-shell');
    const createShell = document.getElementById('create-shell');
    const accountChip = document.getElementById('account-chip');

    if (appState.currentUser) {
        bootShell.hidden = true;
        authShell.hidden = true;
        accountChip.hidden = false;
        accountChip.innerHTML = `
            <span class="account-chip-label">Signed in as</span>
            <strong>${escapeHTML(appState.currentUser.display_name)}</strong>
        `;
        navigateTo(appState.activeView || 'feed');
        refreshApp();
    } else {
        page.dataset.view = 'auth';
        bootShell.hidden = true;
        authShell.hidden = false;
        feedShell.hidden = true;
        if (createShell) createShell.hidden = true;
        accountChip.hidden = true;
    }
}

function renderStats(communities = []) {
    const stats = document.getElementById('dashboard-stats');
    if (!stats) {
        return;
    }

    const subscribed = communities.filter((community) => community.subscribed);
    const cards = [
        { value: subscribed.length, label: 'Subscribed' },
        { value: communities.length, label: 'Communities' },
    ];

    stats.innerHTML = cards.map((card) => `
        <div class="stat-card">
            <span class="stat-value">${escapeHTML(String(card.value))}</span>
            <span class="stat-label">${escapeHTML(card.label)}</span>
        </div>
    `).join('');
}

async function loadModels() {
    const select = document.getElementById('comm-model');
    const editSelect = document.getElementById('edit-community-model');
    select.innerHTML = '<option>Loading models...</option>';
    editSelect.innerHTML = '<option>Loading models...</option>';

    try {
        const data = await fetchJSON('/api/models');
        const models = data.models || [];
        const options = models.length
            ? models.map((model) => `<option value="${escapeHTML(model)}">${escapeHTML(model)}</option>`).join('')
            : '<option value="">No local models found</option>';
        select.innerHTML = options;
        editSelect.innerHTML = options;
    } catch (err) {
        select.innerHTML = '<option value="">Model lookup unavailable</option>';
        editSelect.innerHTML = '<option value="">Model lookup unavailable</option>';
    }
}

async function loadCommunities() {
    const data = await fetchJSON('/api/communities');
    appState.communities = data.communities || [];
    renderStats(appState.communities);
    renderCommunities();
}

function renderCommunities() {
    renderSidebarCommunities();
    renderDiscoveryCommunities();
}

function renderSidebarCommunities() {
    const grid = document.getElementById('communities-grid');
    const summary = document.getElementById('community-summary');
    if (!grid) return;

    const subscribedCount = appState.communities.filter((community) => community.subscribed).length;

    if (summary) {
        summary.textContent = subscribedCount
            ? `${formatCount(subscribedCount, 'subscription')} in your home feed.`
            : 'Subscribe to a few communities to start building your feed.';
    }

    const subscribedCommunities = appState.communities.filter((community) => community.subscribed);

    if (!subscribedCommunities.length) {
        grid.innerHTML = `
            <div class="empty-state">
                <strong>No communities yet</strong>
                Join a room and it will immediately show up here.
            </div>
        `;
        return;
    }

    grid.innerHTML = subscribedCommunities.map((community) => `
        <article class="community-card community-list-item" data-url="/community.html?name=${encodeURIComponent(community.name)}">
            <div class="community-list-main">
                <div class="community-list-header">
                    <h3 class="community-list-title">${escapeHTML(community.name)}</h3>
                </div>
            </div>
        </article>
    `).join('');

    if (window.lucide) {
        lucide.createIcons();
    }
}

function renderDiscoveryCommunities() {
    const grid = document.getElementById('discovery-grid');
    if (!grid) return;

    const query = (appState.communitySearchInput || '').toLowerCase();
    const filtered = appState.communities.filter(c => 
        c.name.toLowerCase().includes(query) || 
        (c.description || '').toLowerCase().includes(query)
    );

    if (!filtered.length) {
        grid.innerHTML = `
            <div class="empty-state discovery-empty">
                <strong>No matches found</strong>
                Try searching for something else or browse the network.
            </div>
        `;
        return;
    }

    grid.innerHTML = filtered.map((community) => `
        <article class="post clickable-card community-discovery-card" data-url="/community.html?name=${encodeURIComponent(community.name)}">
            <div class="post-header">
                <div class="pill-row">
                    <span class="pill pill-accent">${escapeHTML(community.model)}</span>
                    <span class="pill">${escapeHTML(formatCount(community.subscriber_count || 0, 'subscriber'))}</span>
                    <span class="pill">${escapeHTML(`${community.posting_rate}s cycle`)}</span>
                </div>
                <div class="card-topline">
                    <h3>${escapeHTML(community.name)}</h3>
                    <div class="card-actions">
                        <button type="button" class="btn-text subscribe-btn ${community.subscribed ? 'is-active' : ''}" data-action="subscribe" data-name="${escapeHTML(community.name)}" data-subscribed="${community.subscribed ? 'true' : 'false'}">
                            ${community.subscribed ? 'Subscribed' : 'Join Community'}
                        </button>
                    </div>
                </div>
            </div>
            <div class="post-content">${escapeHTML(community.description || 'No description provided.')}</div>
            <div class="card-footer card-footer-stack">
                <div class="card-inline-actions">
                    <button type="button" class="icon-btn" data-action="edit" data-name="${escapeHTML(community.name)}" aria-label="Edit community">
                        <i data-lucide="settings-2"></i>
                    </button>
                    <button type="button" class="icon-btn btn-danger" data-action="delete" data-name="${escapeHTML(community.name)}" aria-label="Delete community">
                        <i data-lucide="trash-2"></i>
                    </button>
                </div>
            </div>
        </article>
    `).join('');

    if (window.lucide) {
        lucide.createIcons();
    }
}

async function loadFeed() {
    const feed = document.getElementById('home-feed');

    try {
        const data = await fetchJSON(`/api/feed?sort=${encodeURIComponent(appState.sort)}`);
        appState.feed = data.posts || [];
        renderFeed();
    } catch (err) {
        feed.innerHTML = `
            <div class="empty-state">
                <strong>Feed unavailable</strong>
                ${escapeHTML(err.message)}
            </div>
        `;
    }
}

function renderFeed() {
    const feed = document.getElementById('home-feed');
    const title = document.getElementById('feed-title');
    const subtitle = document.getElementById('feed-subtitle');

    title.textContent = `${appState.currentUser.display_name}'s feed`;
    subtitle.textContent = appState.feed.length
        ? `${formatCount(appState.feed.length, 'post')} from the communities you follow.`
        : 'Your subscribed communities are quiet right now. Try another sort or visit a room directly.';

    if (!appState.feed.length) {
        feed.innerHTML = `
            <div class="empty-state">
                <strong>No posts yet</strong>
                Subscribe to communities on the right and they will start showing up here.
            </div>
        `;
        return;
    }

    feed.innerHTML = appState.feed.map((post) => `
        <article class="post post-feed-card clickable-card" data-url="/community.html?name=${encodeURIComponent(post.community_name)}#post-${post.id}">
            <div class="post-header">
                <div class="pill-row">
                    <a class="pill pill-accent" href="/community.html?name=${encodeURIComponent(post.community_name)}">${escapeHTML(post.community_name)}</a>
                    <span class="pill">${escapeHTML(`@${post.author}`)}</span>
                    <span class="pill">${escapeHTML(formatTimestamp(post.created_at))}</span>
                    <span class="pill">${escapeHTML(formatCount(post.comment_count, 'reply', 'replies'))}</span>
                    ${(() => {
                        const unreadCount = getUnreadReplyCount(post);
                        const targetId = getFirstUnreadReplyTarget(post);
                        if (!unreadCount || !targetId) {
                            return '';
                        }
                        const unreadLabel = unreadCount === 1 ? 'new reply' : 'new replies';
                        return `<a class="reply-badge" href="/community.html?name=${encodeURIComponent(post.community_name)}#${targetId}">${escapeHTML(`${unreadCount} ${unreadLabel}`)}</a>`;
                    })()}
                </div>
                <div>
                    <h3><a href="/community.html?name=${encodeURIComponent(post.community_name)}#post-${post.id}">${escapeHTML(post.title)}</a></h3>
                    <div class="post-meta">
                        <span>From <a href="/community.html?name=${encodeURIComponent(post.community_name)}"><strong>${escapeHTML(post.community_name)}</strong></a></span>
                    </div>
                </div>
            </div>
            <div class="post-content">${escapeHTML(post.content)}</div>
            <div class="card-footer">
                <span>${escapeHTML(post.community_description || 'Open the community for the full thread.')}</span>
            </div>
        </article>
    `).join('');
}

async function refreshApp() {
    if (appState.activeView === 'feed') {
        await Promise.all([loadCommunities(), loadFeed()]);
    } else {
        await loadCommunities();
    }
}

async function handleLogin(event) {
    event.preventDefault();
    const button = document.getElementById('login-btn');
    const error = document.getElementById('login-error');
    error.textContent = '';
    button.disabled = true;
    button.classList.add('is-busy');

    try {
        await fetchJSON('/api/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                display_name: document.getElementById('login-user').value,
                pin: document.getElementById('login-pin').value,
            }),
        });
        document.getElementById('login-pin').value = '';
        await loadSession();
        if (appState.currentUser) {
            await refreshApp();
        }
    } catch (err) {
        error.textContent = err.message;
    } finally {
        button.disabled = false;
        button.classList.remove('is-busy');
    }
}

async function handleRegister(event) {
    event.preventDefault();
    const button = document.getElementById('register-btn');
    const error = document.getElementById('register-error');
    error.textContent = '';
    button.disabled = true;
    button.classList.add('is-busy');

    try {
        await fetchJSON('/api/register', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                display_name: document.getElementById('register-name').value.trim(),
                pin: document.getElementById('register-pin').value.trim(),
            }),
        });
        document.getElementById('register-form').reset();
        await loadSession();
        if (appState.currentUser) {
            await refreshApp();
        }
    } catch (err) {
        error.textContent = err.message;
    } finally {
        button.disabled = false;
        button.classList.remove('is-busy');
    }
}

async function handleLogout() {
    try {
        await fetchJSON('/api/logout', { method: 'POST' });
        appState.currentUser = null;
        appState.communities = [];
        appState.feed = [];
        writeCachedSession(null);
        window.location.reload();
    } catch (err) {
        alert(err.message);
    }
}

async function handleCreate(event) {
    event.preventDefault();

    const form = event.target;
    const button = document.getElementById('create-community-btn');
    const errorDiv = document.getElementById('create-error');
    const name = document.getElementById('comm-name').value.trim();
    const description = document.getElementById('comm-desc').value.trim();
    const model = document.getElementById('comm-model').value;
    const rate = parseInt(document.getElementById('comm-rate').value, 10);
    const tone = document.getElementById('comm-tone').value;
    const styleNotes = document.getElementById('comm-style').value.trim();

    errorDiv.textContent = '';

    if (!name || !model) {
        errorDiv.textContent = 'Name and model are required.';
        return;
    }

    button.disabled = true;
    button.classList.add('is-busy');

    try {
        await fetchJSON('/api/communities', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, description, model, posting_rate: rate, tone, style_notes: styleNotes }),
        });
        form.reset();
        document.getElementById('comm-tone').value = 'casual';
        updateTonePreview('comm-tone', 'comm-tone-preview');
        await refreshApp();
    } catch (err) {
        errorDiv.textContent = err.message;
    } finally {
        button.disabled = false;
        button.classList.remove('is-busy');
    }
}

async function handleDelete(name) {
    try {
        await fetchJSON(`/api/community/${encodeURIComponent(name)}/delete`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: '{}',
        });
        closeDeleteModal();
        await refreshApp();
    } catch (err) {
        alert(err.message);
    }
}

async function handleEditSave(event) {
    event.preventDefault();
    const elements = getEditModalElements();
    const payload = {
        description: elements.description.value.trim(),
        model: elements.model.value,
        posting_rate: parseInt(elements.rate.value, 10),
        tone: elements.tone.value,
        style_notes: elements.style.value.trim(),
    };

    elements.error.textContent = '';
    elements.save.disabled = true;
    elements.save.classList.add('is-busy');

    try {
        await fetchJSON(`/api/community/${encodeURIComponent(elements.name.value)}/update`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        closeEditModal();
        await refreshApp();
    } catch (err) {
        elements.error.textContent = err.message;
    } finally {
        elements.save.disabled = false;
        elements.save.classList.remove('is-busy');
    }
}

async function toggleSubscription(name, subscribed) {
    await fetchJSON(`/api/community/${encodeURIComponent(name)}/subscribe`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ subscribed }),
    });
    await refreshApp();
}

function initSSE() {
    const eventSource = new EventSource('/api/stream');
    eventSource.addEventListener('new_post', async (event) => {
        try {
            if (appState.activeView === 'feed') {
                await loadFeed();
            }
        } catch (e) {
            console.error('Failed to parse SSE new_post event', e);
        }
    });
    eventSource.addEventListener('new_comment', async (event) => {
        try {
            if (appState.activeView === 'feed') {
                await loadFeed();
            }
        } catch (e) {
            console.error('Failed to parse SSE new_comment event', e);
        }
    });
    eventSource.onerror = () => {
        console.warn('SSE connection lost. It will attempt to reconnect automatically.');
    };
}

document.addEventListener('DOMContentLoaded', async () => {
    initScrollObserver();
    renderBootState();
    initSSE();
    populateToneSelect();

    try {
        const { current_user } = await fetchJSON('/api/session');
        if (current_user) {
            appState.currentUser = current_user;
            writeCachedSession(current_user);
            renderAuthOptions([current_user]);
            renderSessionState();
        } else {
            await loadModels();
            await loadSession();
        }
    } catch (err) {
        await loadModels();
        await loadSession();
    }

    document.getElementById('login-form').addEventListener('submit', handleLogin);
    document.getElementById('register-form').addEventListener('submit', handleRegister);
    document.getElementById('logout-btn').addEventListener('click', handleLogout);
    document.getElementById('new-community-form').addEventListener('submit', handleCreate);
    document.getElementById('edit-community-form').addEventListener('submit', handleEditSave);
    document.getElementById('comm-tone').addEventListener('change', () => updateTonePreview('comm-tone', 'comm-tone-preview'));
    document.getElementById('edit-community-tone').addEventListener('change', () => updateTonePreview('edit-community-tone', 'edit-tone-preview'));

    document.querySelectorAll('.top-nav .nav-link').forEach(link => {
        link.addEventListener('click', (event) => {
            event.preventDefault();
            const target = link.dataset.viewTarget;
            if (target) {
                navigateTo(target);
            }
        });
    });

    document.querySelector('.sort-toggle').addEventListener('click', async (event) => {
        const button = event.target.closest('button[data-sort]');
        if (!button) {
            return;
        }
        const nextSort = button.getAttribute('data-sort');
        if (nextSort === appState.sort) {
            return;
        }
        appState.sort = nextSort;
        document.querySelectorAll('.sort-toggle button').forEach((node) => {
            node.classList.toggle('is-active', node === button);
        });
        await loadFeed();
    });

    const sidebarToggle = document.getElementById('sidebar-toggle');
    if (sidebarToggle) {
        sidebarToggle.addEventListener('click', () => toggleSidebar());
    }

    const sidebarBackdrop = document.getElementById('sidebar-backdrop');
    if (sidebarBackdrop) {
        sidebarBackdrop.addEventListener('click', () => toggleSidebar(false));
    }

    document.getElementById('community-search').addEventListener('input', (event) => {
        appState.communitySearchInput = event.target.value;
        renderDiscoveryCommunities();
    });

    document.getElementById('discovery-grid').addEventListener('click', async (event) => {
        const editButton = event.target.closest('button[data-action="edit"]');
        if (editButton) {
            const community = appState.communities.find((item) => item.name === editButton.getAttribute('data-name'));
            if (community) {
                openEditModal(community);
            }
            return;
        }

        const deleteButton = event.target.closest('button[data-action="delete"]');
        if (deleteButton) {
            openDeleteModal(deleteButton.getAttribute('data-name'));
            return;
        }

        const subscribeButton = event.target.closest('button[data-action="subscribe"]');
        if (subscribeButton) {
            const name = subscribeButton.getAttribute('data-name');
            const subscribed = subscribeButton.getAttribute('data-subscribed') !== 'true';
            await toggleSubscription(name, subscribed);
            return;
        }

        const card = event.target.closest('.community-discovery-card[data-url]');
        if (card && !event.target.closest('a, button')) {
            window.location.href = card.getAttribute('data-url');
        }
    });
    
    document.getElementById('home-feed').addEventListener('click', (event) => {
        const card = event.target.closest('.clickable-card[data-url]');
        if (card && !event.target.closest('a, button')) {
            window.location.href = card.getAttribute('data-url');
        }
    });

    document.getElementById('communities-grid').addEventListener('click', async (event) => {
        const editButton = event.target.closest('button[data-action="edit"]');
        if (editButton) {
            const community = appState.communities.find((item) => item.name === editButton.getAttribute('data-name'));
            if (community) {
                openEditModal(community);
            }
            return;
        }

        const deleteButton = event.target.closest('button[data-action="delete"]');
        if (deleteButton) {
            openDeleteModal(deleteButton.getAttribute('data-name'));
            return;
        }

        const subscribeButton = event.target.closest('button[data-action="subscribe"]');
        if (subscribeButton) {
            const name = subscribeButton.getAttribute('data-name');
            const subscribed = subscribeButton.getAttribute('data-subscribed') !== 'true';
            await toggleSubscription(name, subscribed);
            return;
        }

        const card = event.target.closest('.community-card[data-url]');
        if (card && !event.target.closest('a, button')) {
            toggleSidebar(false);
            window.location.href = card.getAttribute('data-url');
        }
    });

    const deleteModal = document.getElementById('delete-modal');
    deleteModal.addEventListener('click', (event) => {
        if (event.target.matches('[data-close-modal="true"]')) {
            closeDeleteModal();
        }
    });
    document.getElementById('delete-cancel-btn').addEventListener('click', closeDeleteModal);
    document.getElementById('delete-confirm-btn').addEventListener('click', () => {
        if (deleteModalState.name) {
            handleDelete(deleteModalState.name);
        }
    });

    const editModal = document.getElementById('edit-modal');
    editModal.addEventListener('click', (event) => {
        if (event.target.matches('[data-close-edit-modal="true"]')) {
            closeEditModal();
        }
    });
    document.getElementById('edit-cancel-btn').addEventListener('click', closeEditModal);

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && deleteModal.classList.contains('is-open')) {
            closeDeleteModal();
        }
        if (event.key === 'Escape' && editModal.classList.contains('is-open')) {
            closeEditModal();
        }
    });
});
