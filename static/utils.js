function getQueryParam(param) {
    const params = new URLSearchParams(window.location.search);
    return params.get(param);
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
