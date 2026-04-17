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

function getNotificationStorageKey(name) {
    return `community-notifications:${name}`;
}

function getUnreadNotificationStorageKey(name) {
    return `community-unread-notifications:${name}`;
}

function getNotificationPreferenceKey(name, kind) {
    return `user-notification-pref:${kind}`;
}

function getNotificationPreference(name, kind) {
    return localStorage.getItem(getNotificationPreferenceKey(name, kind)) === 'true';
}

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
        if (unread.delete(hash)) {
            persistUnreadNotifications(communityName, unread);
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

function showToast(title, message, targetId = null) {
    const region = document.getElementById('toast-region');
    if (!region) {
        return;
    }

    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.innerHTML = `
        <span class="toast-title">${escapeHTML(title)}</span>
        <div class="toast-copy">${escapeHTML(message)}</div>
        <div class="toast-actions">
            <button type="button" class="btn-text">Dismiss</button>
        </div>
    `;

    const dismiss = () => {
        toast.remove();
    };

    toast.querySelector('button').addEventListener('click', dismiss);
    if (targetId) {
        toast.style.cursor = 'pointer';
        toast.addEventListener('click', (event) => {
            if (event.target.closest('button')) {
                return;
            }
            navigateToTarget(targetId);
            dismiss();
        });
    }
    region.prepend(toast);
    window.setTimeout(dismiss, 7000);
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
    const seen = getSeenNotifications(communityName);
    const fresh = [];

    function walkCommentTree(comments, context) {
        comments.forEach((comment) => {
            const isReplyToUser = context.parentAuthor === 'You' && comment.author !== 'You';
            if (isReplyToUser) {
                const key = `comment:${comment.id}`;
                if (!seen.has(key)) {
                    seen.add(key);
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
            const repliedToUserPost = post.author === 'You' && comment.author !== 'You';
            if (repliedToUserPost) {
                const key = `comment:${comment.id}`;
                if (!seen.has(key)) {
                    seen.add(key);
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
    return fresh;
}

function countComments(comments = []) {
    return comments.reduce((total, comment) => total + 1 + countComments(comment.children || []), 0);
}

function createEmptyState(title, message) {
    return `
        <div class="empty-state">
            <strong>${escapeHTML(title)}</strong>
            ${escapeHTML(message)}
        </div>
    `;
}

function renderComments(comments, container, depth = 0, postId = null) {
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
            <button class="btn-text reply-btn" data-post-id="${postId}" data-parent-id="${comment.id}">
                <i data-lucide="message-square-plus"></i>
                <span>Reply</span>
            </button>
        `;
        container.appendChild(div);

        if (comment.children && comment.children.length > 0) {
            renderComments(comment.children, container, depth + 1, postId);
        }
    });
}

function renderFeed(posts) {
    const feed = document.getElementById('feed');
    const subtitle = document.getElementById('community-subtitle');

    subtitle.textContent = posts.length
        ? `${posts.length} recent thread${posts.length === 1 ? '' : 's'} streaming from this community.`
        : 'No activity yet. Start the first thread and the feed will build from there.';

    if (!posts.length) {
        feed.innerHTML = createEmptyState('No posts yet', 'Publish a thread or wait for the simulation to begin posting.');
        return;
    }

    feed.innerHTML = '';

    posts.forEach((post) => {
        const article = document.createElement('article');
        article.className = 'post reveal-on-load';
        article.id = `post-${post.id}`;
        const totalComments = countComments(post.comments || []);

        article.innerHTML = `
            <div class="post-header">
                <div class="pill-row">
                    <span class="pill pill-accent">${escapeHTML(`@${post.author}`)}</span>
                    <span class="pill">${escapeHTML(formatTimestamp(post.created_at))}</span>
                    <span class="pill">${escapeHTML(`${totalComments} repl${totalComments === 1 ? 'y' : 'ies'}`)}</span>
                </div>
                <div>
                    <h3>${escapeHTML(post.title)}</h3>
                    <div class="post-meta">
                        <span>Posted by <a href="/agent.html?id=${post.agent_id}"><strong>@${escapeHTML(post.author)}</strong></a></span>
                    </div>
                </div>
            </div>
            <div class="post-content">${escapeHTML(post.content)}</div>
            <button class="btn-text reply-btn" data-post-id="${post.id}" data-parent-id="">
                <i data-lucide="message-square-plus"></i>
                <span>Reply</span>
            </button>
            <div class="comments"></div>
        `;

        const commentsContainer = article.querySelector('.comments');
        if (post.comments && post.comments.length > 0) {
            renderComments(post.comments, commentsContainer, 0, post.id);
        }

        feed.appendChild(article);
    });

    if (window.lucide) {
        lucide.createIcons();
    }
}

async function loadFeed(name, options = {}) {
    const feed = document.getElementById('feed');

    try {
        const data = await fetchJSON(`/api/community/${encodeURIComponent(name)}/feed`);
        const posts = data.posts || [];
        renderFeed(posts);

        const notifications = collectUserReplyNotifications(posts, name);
        if (!options.silentNotifications) {
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
        focusTargetFromHash();
    } catch (err) {
        feed.innerHTML = createEmptyState('Feed unavailable', err.message);
    }
}

document.addEventListener('DOMContentLoaded', () => {
    const name = getQueryParam('name');
    const titleEl = document.getElementById('community-title');

    if (!name) {
        titleEl.textContent = 'Community';
        document.getElementById('feed').innerHTML = createEmptyState('No community selected', 'Open a community from the home page.');
        return;
    }

    document.title = `${name} | Local Social Lab`;
    titleEl.textContent = name;
    updateNotificationButtons(name);
    loadFeed(name, { silentNotifications: true });
    window.addEventListener('hashchange', focusTargetFromHash);

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

    setInterval(() => {
        if (document.querySelector('.reply-form')) {
            return;
        }

        const titleVal = document.getElementById('new-post-title').value.trim();
        const contentVal = document.getElementById('new-post-content').value.trim();
        const activeElement = document.activeElement;
        const isTyping = activeElement && (activeElement.id === 'new-post-title' || activeElement.id === 'new-post-content');

        if (titleVal || contentVal || isTyping) {
            return;
        }

        loadFeed(name);
    }, 15000);

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
            loadFeed(name);
        } catch (err) {
            alert(err.message);
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
                loadFeed(name);
            } catch (err) {
                alert(err.message);
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
});
