async function fetchJSON(url, options = {}) {
    let res;
    try {
        res = await fetch(url, options);
    } catch (err) {
        throw new Error('Network error. Check your connection and try again.');
    }

    let data = null;
    try {
        data = await res.json();
    } catch (err) {
        data = null;
    }

    if (!res.ok) {
        throw new Error(data?.error || `Request failed (${res.status})`);
    }
    return data || {};
}




function getNotificationStorageKey(name) {
    const actor = communityPageState.currentUser?.agent_username || 'guest';
    return `community-notifications:${actor}:${name}`;
}

function getUnreadNotificationStorageKey(name) {
    const actor = communityPageState.currentUser?.agent_username || 'guest';
    return `community-unread-notifications:${actor}:${name}`;
}

function getNotificationPreferenceKey(name, kind) {
    const actor = communityPageState.currentUser?.agent_username || 'guest';
    return `user-notification-pref:${actor}:${name}:${kind}`;
}

function getNotificationPreference(name, kind) {
    return localStorage.getItem(getNotificationPreferenceKey(name, kind)) === 'true';
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

const communityPageState = {
    name: null,
    community: null,
    feedOffset: 0,
    feedHasMore: false,
    isLoadingFeed: false,
    currentUser: null,
};

function setNotificationPreference(name, kind, value) {
    localStorage.setItem(getNotificationPreferenceKey(name, kind), value ? 'true' : 'false');
}

function getSeenNotifications(name) {
    try {
        const raw = localStorage.getItem(getNotificationStorageKey(name));
        return new Set(raw ? JSON.parse(raw) : []);
    } catch (err) {
        return new Set();
    }
}

function persistSeenNotifications(name, ids) {
    localStorage.setItem(getNotificationStorageKey(name), JSON.stringify(Array.from(ids)));
}

function getUnreadNotifications(name) {
    try {
        const raw = localStorage.getItem(getUnreadNotificationStorageKey(name));
        return new Set(raw ? JSON.parse(raw) : []);
    } catch (err) {
        return new Set();
    }
}

function persistUnreadNotifications(name, ids) {
    localStorage.setItem(getUnreadNotificationStorageKey(name), JSON.stringify(Array.from(ids)));
}

function targetIdToNotificationKey(targetId) {
    if (targetId.startsWith('comment-')) {
        return `comment:${targetId.slice('comment-'.length)}`;
    }
    return targetId;
}

function getToneOption(value) {
    return toneOptions.find((tone) => tone.value === value) || toneOptions[0];
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

function updateTonePreview() {
    const select = document.getElementById('community-edit-tone');
    const preview = document.getElementById('community-edit-tone-preview');
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
    const toneSelect = document.getElementById('community-edit-tone');
    if (!toneSelect || toneSelect.childElementCount) {
        updateTonePreview();
        return;
    }

    toneSelect.innerHTML = toneOptions.map((tone) => `
        <option value="${tone.value}">${tone.label} - ${tone.description}</option>
    `).join('');
    toneSelect.value = 'casual';
    updateTonePreview();
}

function getEditModalElements() {
    return {
        shell: document.getElementById('community-edit-modal'),
        description: document.getElementById('community-edit-description'),
        model: document.getElementById('community-edit-model'),
        rate: document.getElementById('community-edit-rate'),
        tone: document.getElementById('community-edit-tone'),
        style: document.getElementById('community-edit-style'),
        error: document.getElementById('community-edit-error'),
        save: document.getElementById('community-edit-save'),
    };
}

async function loadModelsIntoEditModal() {
    const modelSelect = document.getElementById('community-edit-model');
    if (!modelSelect) {
        return;
    }

    modelSelect.innerHTML = '<option>Loading models...</option>';

    try {
        const data = await fetchJSON('/api/models');
        modelSelect.innerHTML = '';

        if (!data.models.length) {
            modelSelect.innerHTML = '<option value="">No local models found</option>';
            return;
        }

        data.models.forEach((model) => {
            const option = document.createElement('option');
            option.value = model;
            option.textContent = model;
            modelSelect.appendChild(option);
        });
    } catch (err) {
        modelSelect.innerHTML = '<option value="">Model lookup unavailable</option>';
    }
}

async function loadCommunityDetails(name) {
    const data = await fetchJSON('/api/communities');
    const community = (data.communities || []).find((entry) => entry.name === name);
    if (!community) {
        throw new Error('Community not found');
    }
    communityPageState.community = community;
    return community;
}

function openEditModal() {
    const community = communityPageState.community;
    const elements = getEditModalElements();
    if (!community || !elements.shell) {
        return;
    }

    elements.description.value = community.description || '';
    elements.rate.value = community.posting_rate || 60;
    elements.tone.value = community.tone || 'casual';
    elements.style.value = community.style_notes || '';
    elements.error.textContent = '';

    if (community.model) {
        elements.model.value = community.model;
    }

    updateTonePreview();
    elements.shell.hidden = false;
    elements.shell.classList.add('is-open');
    elements.shell.setAttribute('aria-hidden', 'false');
    elements.description.focus();
}

function closeEditModal() {
    const elements = getEditModalElements();
    if (!elements.shell) {
        return;
    }
    elements.shell.classList.remove('is-open');
    elements.shell.setAttribute('aria-hidden', 'true');
    elements.shell.hidden = true;
}

async function saveCommunityEdits(event) {
    event.preventDefault();

    const elements = getEditModalElements();
    const name = communityPageState.name;
    if (!name) {
        return;
    }

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
        await fetchJSON(`/api/community/${encodeURIComponent(name)}/update`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        await loadCommunityDetails(name);
        syncCommunityHeader();
        closeEditModal();
        showToast('Community updated', 'The room settings were saved.');
    } catch (err) {
        elements.error.textContent = err.message;
    } finally {
        elements.save.disabled = false;
        elements.save.classList.remove('is-busy');
    }
}

function syncCommunityHeader() {
    const community = communityPageState.community;
    const titleEl = document.getElementById('community-title');
    const subtitle = document.getElementById('community-subtitle');
    const subscribeButton = document.getElementById('community-subscribe-btn');
    if (!community || !titleEl || !subtitle) {
        return;
    }

    titleEl.textContent = community.name;
    subtitle.textContent = community.description
        ? community.description
        : 'Live conversation from your local simulation.';

    // AMA Banner
    let amaBanner = document.getElementById('ama-banner');
    if (community.active_ama_agent_id) {
        if (!amaBanner) {
            amaBanner = document.createElement('div');
            amaBanner.id = 'ama-banner';
            amaBanner.className = 'ama-banner';
            amaBanner.innerHTML = `<span class="pulsing-dot"></span> <strong>Live AMA in Progress!</strong> An agent is currently hosting an AMA in this community.`;
            document.querySelector('.feed-header-copy').appendChild(amaBanner);
        }
    } else if (amaBanner) {
        amaBanner.remove();
    }
    if (subscribeButton) {
        const subscribed = !!community.subscribed;
        subscribeButton.querySelector('span').textContent = subscribed ? 'Subscribed' : 'Subscribe';
        subscribeButton.classList.toggle('is-active', subscribed);
    }
}


function playNotificationSound() {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) {
        return;
    }

    const context = new AudioContextClass();
    const oscillator = context.createOscillator();
    const gain = context.createGain();

    oscillator.type = 'sine';
    oscillator.frequency.setValueAtTime(740, context.currentTime);
    oscillator.frequency.exponentialRampToValueAtTime(520, context.currentTime + 0.18);
    gain.gain.setValueAtTime(0.0001, context.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.08, context.currentTime + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, context.currentTime + 0.24);

    oscillator.connect(gain);
    gain.connect(context.destination);
    oscillator.start();
    oscillator.stop(context.currentTime + 0.26);
    oscillator.onended = () => context.close();
}

async function ensureBrowserNotificationPermission() {
    if (!('Notification' in window)) {
        return false;
    }

    if (Notification.permission === 'granted') {
        return true;
    }

    if (Notification.permission === 'denied') {
        return false;
    }

    const permission = await Notification.requestPermission();
    return permission === 'granted';
}

function sendBrowserNotification(title, message, targetId = null) {
    if (!('Notification' in window) || Notification.permission !== 'granted') {
        return;
    }

    const notification = new Notification(title, {
        body: message,
        tag: 'community-reply',
    });
    notification.onclick = () => {
        window.focus();
        navigateToTarget(targetId);
        notification.close();
    };
    window.setTimeout(() => notification.close(), 6000);
}

function updateNotificationButtons(name) {
    const soundEnabled = getNotificationPreference(name, 'sound');
    const browserEnabled = getNotificationPreference(name, 'browser');
    const soundButton = document.getElementById('sound-toggle-btn');
    const browserButton = document.getElementById('browser-toggle-btn');

    soundButton.textContent = soundEnabled ? 'Sound on' : 'Sound off';
    soundButton.classList.toggle('is-active', soundEnabled);
    browserButton.textContent = browserEnabled ? 'Browser alerts on' : 'Browser alerts off';
    browserButton.classList.toggle('is-active', browserEnabled);
}

function collectUserReplyNotifications(posts, communityName) {
    const currentHandle = communityPageState.currentUser?.agent_username;
    if (!currentHandle) {
        return [];
    }
    const seen = getSeenNotifications(communityName);
    const unread = getUnreadNotifications(communityName);
    const fresh = [];

    function walkCommentTree(comments, context) {
        comments.forEach((comment) => {
            const isReplyToUser = context.parentAuthor === currentHandle && comment.author !== currentHandle;
            if (isReplyToUser) {
                const key = `comment:${comment.id}`;
                if (!seen.has(key)) {
                    seen.add(key);
                    unread.add(key);
                    fresh.push({
                        title: `${comment.author} replied to you`,
                        message: `In "${context.postTitle}"`,
                        targetId: `comment-${comment.id}`,
                    });
                }
            }

            walkCommentTree(comment.children || [], {
                postTitle: context.postTitle,
                parentAuthor: comment.author,
            });
        });
    }

    posts.forEach((post) => {
        (post.comments || []).forEach((comment) => {
            const repliedToUserPost = post.author === currentHandle && comment.author !== currentHandle;
            if (repliedToUserPost) {
                const key = `comment:${comment.id}`;
                if (!seen.has(key)) {
                    seen.add(key);
                    unread.add(key);
                    fresh.push({
                        title: `${comment.author} replied to your post`,
                        message: `On "${post.title}"`,
                        targetId: `comment-${comment.id}`,
                    });
                }
            }
        });

        walkCommentTree(post.comments || [], {
            postTitle: post.title,
            parentAuthor: post.author,
        });
    });

    persistSeenNotifications(communityName, seen);
    persistUnreadNotifications(communityName, unread);
    return fresh;
}

function countComments(comments = []) {
    return comments.reduce((total, comment) => total + 1 + countComments(comment.children || []), 0);
}

function collectUnreadReplyTargets(comments = [], unreadSet, results = []) {
    comments.forEach((comment) => {
        if (unreadSet.has(`comment:${comment.id}`)) {
            results.push(`comment-${comment.id}`);
        }
        collectUnreadReplyTargets(comment.children || [], unreadSet, results);
    });
    return results;
}

function createEmptyState(title, message) {
    return `
        <div class="empty-state">
            <strong>${escapeHTML(title)}</strong>
            ${escapeHTML(message)}
        </div>
    `;
}

function renderComments(comments, container, depth = 0, postId = null, isLocked = false) {
    comments.forEach((comment) => {
        const div = document.createElement('div');
        div.className = 'comment';
        div.id = `comment-${comment.id}`;
        div.style.setProperty('--depth', depth);
        div.innerHTML = `
            <div class="comment-meta">
                <a href="/agent.html?id=${comment.agent_id}"><strong>@${escapeHTML(comment.author)}</strong></a>
                <span>${escapeHTML(formatTimestamp(comment.created_at))}</span>
            </div>
            <div class="post-content">${escapeHTML(comment.content)}</div>
            ${isLocked ? '' : `<button class="btn-text reply-btn" data-post-id="${postId}" data-parent-id="${comment.id}">
                <i data-lucide="message-square-plus"></i>
                <span>Reply</span>
            </button>`}
        `;
        container.appendChild(div);

        if (comment.children && comment.children.length > 0) {
            renderComments(comment.children, container, depth + 1, postId, isLocked);
        }
    });
}

let communityFeedObserver = null;

function renderFeed(posts, isLoadMore = false) {
    const feed = document.getElementById('feed');
    const unread = communityPageState.name ? getUnreadNotifications(communityPageState.name) : new Set();

    if (!isLoadMore) {
        syncCommunityHeader();

        if (!posts.length) {
            feed.innerHTML = createEmptyState('No posts yet', 'Publish a thread or wait for the simulation to begin posting.');
            return;
        }

        feed.innerHTML = '';
    }

    // Remove old sentinel
    const oldSentinel = document.getElementById('feed-sentinel');
    if (oldSentinel) {
        oldSentinel.remove();
    }

    posts.forEach((post) => {
        const article = document.createElement('article');
        article.className = 'post reveal-on-load';
        article.id = `post-${post.id}`;
        const totalComments = countComments(post.comments || []);
        const unreadTargets = collectUnreadReplyTargets(post.comments || [], unread);
        const unreadLabel = unreadTargets.length === 1 ? 'new reply' : 'new replies';

        article.innerHTML = `
            <div class="post-header">
                <div class="pill-row">
                    <span class="pill pill-accent">${escapeHTML(`@${post.author}`)}</span>
                    <span class="pill">${escapeHTML(formatTimestamp(post.created_at))}</span>
                    <span class="pill">${escapeHTML(`${totalComments} repl${totalComments === 1 ? 'y' : 'ies'}`)}</span>
                    ${post.locked ? '<span class="pill pill-danger" style="color: var(--danger-color);"><i data-lucide="lock"></i> Locked</span>' : ''}
                    ${unreadTargets.length ? `<button type="button" class="reply-badge" data-target-id="${unreadTargets[0]}">${escapeHTML(`${unreadTargets.length} ${unreadLabel}`)}</button>` : ''}
                </div>
                <div>
                    <h3>${escapeHTML(post.title)}</h3>
                    <div class="post-meta">
                        <span>Posted by <a href="/agent.html?id=${post.agent_id}"><strong>@${escapeHTML(post.author)}</strong></a></span>
                    </div>
                </div>
            </div>
            ${post.media_url ? `<div class="post-media-attachment" style="background: var(--bg-panel); border: 1px dashed var(--border-subtle); padding: 1rem; border-radius: var(--radius-sm); margin-bottom: 1rem; font-style: italic; color: var(--text-muted);"><i data-lucide="image"></i> ${escapeHTML(post.media_url)}</div>` : ''}
            <div class="post-content">${escapeHTML(post.content)}</div>
            ${post.locked ? '<span class="locked-text" style="color: var(--text-muted); font-size: 0.9rem; padding: 0.5rem 1rem; display: inline-flex; align-items: center; gap: 0.4rem;"><i data-lucide="lock"></i> Thread Locked</span>' : `<button class="btn-text reply-btn" data-post-id="${post.id}" data-parent-id="">
                <i data-lucide="message-square-plus"></i>
                <span>Reply</span>
            </button>`}
            <div class="comments"></div>
        `;

        const commentsContainer = article.querySelector('.comments');
        if (post.comments && post.comments.length > 0) {
            renderComments(post.comments, commentsContainer, 0, post.id, post.locked);
        }

        feed.appendChild(article);
    });

    if (communityPageState.feedHasMore) {
        const sentinel = document.createElement('div');
        sentinel.id = 'feed-sentinel';
        sentinel.className = 'feed-loader';
        sentinel.innerHTML = `<i data-lucide="loader-2" class="spin"></i> Loading more posts...`;
        feed.appendChild(sentinel);
        if (window.lucide) {
            lucide.createIcons({ root: sentinel });
        }

        if (communityFeedObserver) {
            communityFeedObserver.disconnect();
        }

        communityFeedObserver = new IntersectionObserver((entries) => {
            if (entries[0].isIntersecting && !communityPageState.isLoadingFeed) {
                loadFeed(communityPageState.name, { isLoadMore: true });
            }
        });
        communityFeedObserver.observe(sentinel);
    }

    if (window.lucide) {
        lucide.createIcons();
    }
}

async function loadFeed(name, options = {}) {
    const feed = document.getElementById('feed');
    if (communityPageState.isLoadingFeed) return;

    const {
        silentNotifications = false,
        preserveScroll = false,
        focusTarget = false,
        renderDOM = true,
        isLoadMore = false,
    } = options;

    if (!isLoadMore) {
        communityPageState.feedOffset = 0;
    } else {
        communityPageState.feedOffset += 50;
    }

    communityPageState.isLoadingFeed = true;

    const previousScrollY = preserveScroll ? window.scrollY : null;
    const hash = window.location.hash.replace('#', '').trim();
    const targetPostMatch = hash.match(/^post-(\d+)$/);
    const targetPostId = targetPostMatch ? targetPostMatch[1] : '';

    let requestUrl = targetPostId && !isLoadMore
        ? `/api/community/${encodeURIComponent(name)}/feed?target_post_id=${encodeURIComponent(targetPostId)}`
        : `/api/community/${encodeURIComponent(name)}/feed`;

    requestUrl += `${requestUrl.includes('?') ? '&' : '?'}offset=${communityPageState.feedOffset}&limit=50`;

    try {
        const data = await fetchJSON(requestUrl);
        const posts = data.posts || [];
        communityPageState.feedHasMore = data.has_more || false;

        if (data.community) {
            communityPageState.community = data.community;
        }
        communityPageState.currentUser = data.current_user || communityPageState.currentUser;

        if (renderDOM) {
            renderFeed(posts, isLoadMore);
        }

        const notifications = collectUserReplyNotifications(posts, name);
        if (!silentNotifications) {
            notifications.forEach((notification) => {
                showToast(notification.title, notification.message, notification.targetId);
                if (getNotificationPreference(name, 'sound')) {
                    playNotificationSound();
                }
                if (getNotificationPreference(name, 'browser')) {
                    sendBrowserNotification(notification.title, notification.message, notification.targetId);
                }
            });
        }

        if (renderDOM && preserveScroll && previousScrollY !== null) {
            window.scrollTo({ top: previousScrollY, left: 0, behavior: 'auto' });
        }
        if (renderDOM && focusTarget) {
            focusTargetFromHash();
        }
    } catch (err) {
        if (renderDOM && !isLoadMore) {
            feed.innerHTML = createEmptyState('Feed unavailable', err.message);
        }
    } finally {
        communityPageState.isLoadingFeed = false;
    }
}

document.addEventListener('DOMContentLoaded', async () => {
    initScrollObserver();
    const name = getQueryParam('name');
    const titleEl = document.getElementById('community-title');

    if (!name) {
        titleEl.textContent = 'Community';
        document.getElementById('feed').innerHTML = createEmptyState('No community selected', 'Open a community from the home page.');
        return;
    }

    communityPageState.name = name;
    document.title = `${name} | Local Social Lab`;
    updateNotificationButtons(name);
    populateToneSelect();
    loadModelsIntoEditModal();
    loadCommunityDetails(name)
        .then(() => syncCommunityHeader())
        .catch((err) => {
            titleEl.textContent = name;
            document.getElementById('community-subtitle').textContent = err.message;
        });
    await loadFeed(name, { silentNotifications: true, focusTarget: true });
    await loadCommunityState();
    window.addEventListener('hashchange', focusTargetFromHash);

    document.getElementById('community-settings-btn').addEventListener('click', openEditModal);

    const editForm = document.getElementById('community-edit-form');
    if (editForm) {
        editForm.addEventListener('submit', saveCommunityEdits);
    }

    document.getElementById('community-subscribe-btn').addEventListener('click', async () => {
        if (!communityPageState.community) {
            return;
        }

        const nextSubscribed = !communityPageState.community.subscribed;
        const previous = !!communityPageState.community.subscribed;
        communityPageState.community.subscribed = nextSubscribed;
        syncCommunityHeader();

        try {
            let succeeded = false;
            for (let attempt = 0; attempt < 2; attempt += 1) {
                try {
                    await fetchJSON(`/api/community/${encodeURIComponent(name)}/subscribe`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ subscribed: nextSubscribed }),
                    });
                    succeeded = true;
                    break;
                } catch (err) {
                    if (attempt === 0) {
                        await new Promise((resolve) => setTimeout(resolve, 600));
                        continue;
                    }
                    throw err;
                }
            }
            if (!succeeded) {
                throw new Error('Subscription request failed.');
            }
        } catch (err) {
            communityPageState.community.subscribed = previous;
            syncCommunityHeader();
            showToast('Subscription update failed', err.message);
        }
    });
    document.getElementById('community-edit-tone').addEventListener('change', updateTonePreview);
    document.getElementById('community-edit-cancel').addEventListener('click', closeEditModal);
    document.getElementById('community-delete-btn').addEventListener('click', async () => {
        if (!communityPageState.name) return;
        if (!confirm(`Are you sure you want to delete "${communityPageState.name}"? This will remove all simulations and data for this room.`)) {
            return;
        }

        try {
            await fetchJSON(`/api/community/${encodeURIComponent(communityPageState.name)}/delete`, {
                method: 'POST',
            });
            window.location.href = '/';
        } catch (err) {
            showToast('Deletion failed', err.message);
        }
    });

    const editModal = document.getElementById('community-edit-modal');
    editModal.addEventListener('click', (event) => {
        if (event.target.matches('[data-close-community-edit="true"]')) {
            closeEditModal();
        }
    });

    document.getElementById('sound-toggle-btn').addEventListener('click', () => {
        const next = !getNotificationPreference(name, 'sound');
        setNotificationPreference(name, 'sound', next);
        updateNotificationButtons(name);
    });

    document.getElementById('browser-toggle-btn').addEventListener('click', async () => {
        const enabled = getNotificationPreference(name, 'browser');
        if (!enabled) {
            const granted = await ensureBrowserNotificationPermission();
            if (!granted) {
                showToast('Browser alerts unavailable', 'Permission was blocked or not supported here.');
                return;
            }
        }
        setNotificationPreference(name, 'browser', !enabled);
        updateNotificationButtons(name);
    });

    // Live updates via SSE
    function initSSE() {
        let eventSource = null;
        let reconnectAttempts = 0;
        const maxReconnectDelay = 30000; // 30 seconds max delay
        const baseDelay = 1000;
        let sseRefreshTimeout = null;

        const shouldRefreshForEvent = (eventData) => (
            communityPageState.community
            && Number(communityPageState.community.id) === Number(eventData.community_id)
        );

        const scheduleSseRefresh = () => {
            if (sseRefreshTimeout) {
                return;
            }
            sseRefreshTimeout = window.setTimeout(async () => {
                sseRefreshTimeout = null;
                await window.loadCommunityData();
            }, 120);
        };

        function connect() {
            if (eventSource) {
                eventSource.close();
            }

            eventSource = new EventSource('/api/stream');

            eventSource.addEventListener('new_post', async (event) => {
                try {
                    const data = JSON.parse(event.data);
                    if (shouldRefreshForEvent(data)) {
                        scheduleSseRefresh();
                    }
                } catch (e) {
                    console.error('Failed to parse SSE new_post event', e);
                }
            });

            eventSource.addEventListener('new_comment', async (event) => {
                try {
                    const data = JSON.parse(event.data);
                    if (shouldRefreshForEvent(data)) {
                        scheduleSseRefresh();
                    }
                } catch (e) {
                    console.error('Failed to parse SSE new_comment event', e);
                }
            });

            eventSource.onopen = () => {
                reconnectAttempts = 0;
            };

            eventSource.onerror = () => {
                eventSource.close();
                const delay = Math.min(baseDelay * Math.pow(2, reconnectAttempts), maxReconnectDelay);
                console.warn(`SSE connection lost. Reconnecting in ${delay}ms...`);
                reconnectAttempts++;
                setTimeout(connect, delay);
            };
        }

        connect();
    }

    initSSE();

    async function loadCommunityState() {
        if (!communityPageState.community) return;
        try {
            const data = await fetchJSON(`/api/community_state/${communityPageState.community.id}`);
            const panel = document.getElementById('community-state-panel');
            if (panel && data) {
                panel.style.display = 'block';
                document.getElementById('state-energy').textContent = parseFloat(data.energy).toFixed(2);
                document.getElementById('state-mood').textContent = parseFloat(data.mood).toFixed(2);
                document.getElementById('state-conflict').textContent = parseFloat(data.conflict_level).toFixed(2);

                const topics = data.top_topics.map(t => t.topic).join(', ');
                document.getElementById('state-topics').textContent = topics || 'None';
                if (window.lucide) {
                    window.lucide.createIcons();
                }
            }
        } catch (e) {
            console.warn('Failed to load community state', e);
        }
    }

    window.loadCommunityData = async function() {
        const titleVal = document.getElementById('new-post-title').value.trim();
        const contentVal = document.getElementById('new-post-content').value.trim();
        const activeElement = document.activeElement;
        const isTyping = activeElement && (activeElement.id === 'new-post-title' || activeElement.id === 'new-post-content');

        // We fetch the data but only re-render if the user isn't actively writing.
        // This keeps notifications flowing but prevents wiping their typed text.
        const shouldRender = !document.querySelector('.reply-form') && !titleVal && !contentVal && !isTyping;
        await loadFeed(name, { preserveScroll: true, renderDOM: shouldRender });
        await loadCommunityState();
    };

    async function refreshCommunityAfterLocalAction(options = {}) {
        await loadFeed(name, options);
        await loadCommunityState();
    }

    async function submitPost() {
        const button = document.getElementById('submit-post-btn');
        const title = document.getElementById('new-post-title').value.trim();
        const content = document.getElementById('new-post-content').value.trim();

        if (!title || !content) {
            return;
        }

        button.disabled = true;
        button.classList.add('is-busy');

        try {
            await fetchJSON(`/api/community/${encodeURIComponent(name)}/post`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ title, content }),
            });
            document.getElementById('new-post-title').value = '';
            document.getElementById('new-post-content').value = '';
            await refreshCommunityAfterLocalAction({ focusTarget: true });
        } catch (err) {
            showToast('Unable to publish post', err.message);
        } finally {
            button.disabled = false;
            button.classList.remove('is-busy');
        }
    }

    document.getElementById('submit-post-btn').addEventListener('click', submitPost);

    document.getElementById('new-post-content').addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault();
            submitPost();
        }
    });

    document.getElementById('feed').addEventListener('click', async (event) => {
        const replyBadge = event.target.closest('.reply-badge');
        if (replyBadge) {
            navigateToTarget(replyBadge.getAttribute('data-target-id'));
            return;
        }

        const replyButton = event.target.closest('.reply-btn');
        if (!replyButton) {
            return;
        }

        const postId = replyButton.getAttribute('data-post-id');
        const parentId = replyButton.getAttribute('data-parent-id') || null;

        if (replyButton.nextElementSibling && replyButton.nextElementSibling.classList.contains('reply-form')) {
            replyButton.nextElementSibling.remove();
            return;
        }

        const formDiv = document.createElement('div');
        formDiv.className = 'reply-form';
        formDiv.innerHTML = `
            <textarea rows="4" placeholder="Write a reply..."></textarea>
            <div class="reply-actions">
                <button class="btn-text cancel-reply-btn" type="button">Cancel</button>
                <button class="btn-primary submit-reply-btn" type="button">Send reply</button>
            </div>
        `;
        replyButton.parentNode.insertBefore(formDiv, replyButton.nextSibling);

        const textarea = formDiv.querySelector('textarea');
        textarea.focus();

        async function submitReply() {
            const content = textarea.value.trim();
            if (!content) {
                return;
            }

            const submitButton = formDiv.querySelector('.submit-reply-btn');
            submitButton.disabled = true;
            submitButton.classList.add('is-busy');

            try {
                await fetchJSON(`/api/post/${postId}/comment`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ content, parent_id: parentId ? parseInt(parentId, 10) : null }),
                });
                await refreshCommunityAfterLocalAction({ preserveScroll: true });
            } catch (err) {
                showToast('Unable to publish reply', err.message);
            } finally {
                submitButton.disabled = false;
                submitButton.classList.remove('is-busy');
            }
        }

        formDiv.querySelector('.submit-reply-btn').addEventListener('click', submitReply);
        formDiv.querySelector('.cancel-reply-btn').addEventListener('click', () => formDiv.remove());

        textarea.addEventListener('keydown', (keyEvent) => {
            if (keyEvent.key === 'Enter' && !keyEvent.shiftKey) {
                keyEvent.preventDefault();
                submitReply();
            }
        });
    });

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && editModal.classList.contains('is-open')) {
            closeEditModal();
        }
    });
});

function focusTargetFromHash() {
    const hash = window.location.hash.replace('#', '').trim();
    if (!hash) {
        return;
    }

    document.querySelectorAll('.is-targeted').forEach((node) => node.classList.remove('is-targeted'));
    const target = document.getElementById(hash);
    if (!target) {
        return;
    }

    const communityName = getQueryParam('name');
    if (communityName) {
        const unread = getUnreadNotifications(communityName);
        if (unread.delete(targetIdToNotificationKey(hash))) {
            persistUnreadNotifications(communityName, unread);
            loadFeed(communityName, { silentNotifications: true });
        }
    }

    target.classList.add('is-targeted');
    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    window.setTimeout(() => target.classList.remove('is-targeted'), 2200);
}

function navigateToTarget(targetId) {
    if (!targetId) {
        return;
    }

    const nextHash = `#${targetId}`;
    if (window.location.hash !== nextHash) {
        history.replaceState(null, '', nextHash);
    }
    focusTargetFromHash();
}
