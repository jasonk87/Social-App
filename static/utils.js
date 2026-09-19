function getQueryParam(param) {
    const params = new URLSearchParams(window.location.search);
    return params.get(param);
}

function escapeHTML(value = '') {
    return String(value ?? '').replace(/[&<>"']/g, (char) => ({
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
    if (targetId && typeof navigateToTarget === 'function') {
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

async function fetchJSON(url, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
        const res = await fetch(url, { ...options, signal: options.signal || controller.signal });
        const data = await res.json();
        if (!res.ok) {
            const error = new Error(data?.error || `Request failed (${res.status})`);
            error.status = res.status;
            throw error;
        }
        return data;
    } catch (err) {
        if (err.name === 'AbortError') throw new Error('Request timed out. Please try again.');
        if (err instanceof TypeError) throw new Error('Network error. Check your connection and try again.');
        if (err instanceof SyntaxError) throw new Error('The server returned an invalid response. Please try again.');
        throw err;
    } finally {
        clearTimeout(timer);
    }
}

const dialogTriggers = new WeakMap();
function activateDialog(shell) {
    dialogTriggers.set(shell, document.activeElement);
    document.body.classList.add('has-open-dialog');
    document.querySelectorAll('body > header, body > main').forEach(el => { el.inert = true; });
}
function deactivateDialog(shell) {
    document.body.classList.remove('has-open-dialog');
    document.querySelectorAll('body > header, body > main').forEach(el => { el.inert = false; });
    dialogTriggers.get(shell)?.focus();
}
document.addEventListener('keydown', (event) => {
    if (event.key !== 'Tab') return;
    const dialog = document.querySelector('.modal-shell.is-open [role="dialog"]');
    if (!dialog) return;
    const focusable = [...dialog.querySelectorAll('button, input, select, textarea, a[href]')]
        .filter(el => !el.disabled && el.getClientRects().length);
    const first = focusable[0];
    const last = focusable.at(-1);
    if (event.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) {
        event.preventDefault();
        last?.focus();
    } else if (!event.shiftKey && (document.activeElement === last || !dialog.contains(document.activeElement))) {
        event.preventDefault();
        first?.focus();
    }
});
function selectSavedModel(select, model) {
    if (model && ![...select.options].some(option => option.value === model)) {
        select.add(new Option(`${model} (saved model)`, model));
    }
    select.value = model || '';
}
function showFeedUpdate(container, refresh) {
    if (container.querySelector('.feed-update')) return;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'feed-update btn-primary';
    button.textContent = 'New activity · Refresh feed';
    button.addEventListener('click', () => { button.remove(); refresh(); });
    container.prepend(button);
}

function safeSourceURL(value) {
    try {
        const url = new URL(value);
        return url.protocol === 'https:' && !url.username && !url.password ? url.href : null;
    } catch { return null; }
}

function renderPostSources(sources) {
    if (!Array.isArray(sources)) return '';
    const links = sources.slice(0, 5).map(source => {
        const url = safeSourceURL(source?.url);
        return url ? `<a href="${escapeHTML(url)}" target="_blank" rel="noopener noreferrer">${escapeHTML(source.title || new URL(url).hostname)}</a>` : '';
    }).filter(Boolean);
    return links.length ? `<div class="post-sources"><span>From the shared briefing</span>${links.join('')}</div>` : '';
}
