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

function formatCount(count, singular, plural = `${singular}s`) {
    return `${count} ${count === 1 ? singular : plural}`;
}

const deleteModalState = {
    name: null,
};

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

const editModalState = {
    name: null,
};

function getDeleteModalElements() {
    return {
        shell: document.getElementById('delete-modal'),
        copy: document.getElementById('delete-modal-copy'),
        confirm: document.getElementById('delete-confirm-btn'),
        cancel: document.getElementById('delete-cancel-btn'),
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
    elements.shell.setAttribute('aria-hidden', 'true');
    elements.shell.hidden = true;
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

function syncModelOptionsIntoEditModal() {
    const source = document.getElementById('comm-model');
    const target = document.getElementById('edit-community-model');
    if (!source || !target) {
        return;
    }
    target.innerHTML = source.innerHTML;
}

function openEditModal(community) {
    const elements = getEditModalElements();
    editModalState.name = community.name;
    elements.name.value = community.name;
    elements.description.value = community.description || '';
    elements.rate.value = community.posting_rate || 60;
    elements.tone.value = community.tone || 'casual';
    elements.style.value = community.style_notes || '';
    elements.error.textContent = '';
    syncModelOptionsIntoEditModal();
    elements.model.value = community.model || '';
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
    elements.shell.setAttribute('aria-hidden', 'true');
    elements.shell.hidden = true;
}

function renderStats(communities = []) {
    const stats = document.getElementById('dashboard-stats');
    const models = new Set(communities.map((community) => community.model).filter(Boolean));
    const fastestTick = communities.length > 0
        ? `${Math.min(...communities.map((community) => community.posting_rate))}s`
        : '--';

    const cards = [
        { value: communities.length, label: 'Communities live' },
        { value: models.size, label: 'Models in rotation' },
        { value: fastestTick, label: 'Fastest cadence' },
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
    select.innerHTML = '<option>Loading models...</option>';

    try {
        const data = await fetchJSON('/api/models');
        select.innerHTML = '';

        if (!data.models.length) {
            select.innerHTML = '<option value="">No local models found</option>';
            return;
        }

        data.models.forEach((model) => {
            const opt = document.createElement('option');
            opt.value = model;
            opt.textContent = model;
            select.appendChild(opt);
        });
        syncModelOptionsIntoEditModal();
    } catch (err) {
        console.error('Failed to load models:', err);
        select.innerHTML = '<option value="">Model lookup unavailable</option>';
        syncModelOptionsIntoEditModal();
    }
}

async function loadCommunities() {
    const grid = document.getElementById('communities-grid');
    const summary = document.getElementById('community-summary');

    try {
        const data = await fetchJSON('/api/communities');
        const communities = data.communities || [];
        window.__communities = communities;

        renderStats(communities);
        summary.textContent = communities.length
            ? `${formatCount(communities.length, 'community', 'communities')} currently running locally.`
            : 'Create your first community to start the network.';

        if (!communities.length) {
            grid.innerHTML = `
                <div class="empty-state">
                    <strong>No communities yet</strong>
                    Start one above and it will show up here with its model and cadence.
                </div>
            `;
            return;
        }

        grid.innerHTML = communities.map((comm, index) => {
            const url = `/community.html?name=${encodeURIComponent(comm.name)}`;
            return `
                <article class="community-card reveal-on-load" data-url="${url}" style="animation-delay:${index * 70}ms;">
                    <div class="card-topline">
                        <div>
                            <h3>${escapeHTML(comm.name)}</h3>
                        </div>
                        <div class="card-actions">
                            <button class="icon-btn" data-action="edit" data-name="${escapeHTML(comm.name)}" title="Edit community" aria-label="Edit community">
                                <i data-lucide="settings-2"></i>
                            </button>
                            <button class="icon-btn btn-danger" data-action="delete" data-name="${escapeHTML(comm.name)}" title="Delete community" aria-label="Delete community">
                                <i data-lucide="trash-2"></i>
                            </button>
                        </div>
                    </div>
                    <div class="pill-row">
                        <span class="pill pill-accent">${escapeHTML(comm.model)}</span>
                        <span class="pill">${escapeHTML(comm.tone || 'casual')}</span>
                        <span class="pill">${escapeHTML(`${comm.posting_rate}s cadence`)}</span>
                    </div>
                    <p>${escapeHTML(comm.description || 'A fresh local simulation space ready to generate posts and replies.')}</p>
                    <div class="card-footer">
                        <span>${comm.active ? 'Simulation active' : 'Auto-running on launch'}</span>
                        <span>Open feed</span>
                    </div>
                </article>
            `;
        }).join('');

        if (window.lucide) {
            lucide.createIcons();
        }
    } catch (err) {
        console.error('Failed to load communities:', err);
        grid.innerHTML = `
            <div class="empty-state">
                <strong>Unable to load communities</strong>
                ${escapeHTML(err.message)}
            </div>
        `;
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
        await loadCommunities();
    } catch (err) {
        alert(`Error: ${err.message}`);
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
        await loadCommunities();
    } catch (err) {
        elements.error.textContent = err.message;
    } finally {
        elements.save.disabled = false;
        elements.save.classList.remove('is-busy');
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
    button.innerHTML = '<span>Initializing...</span>';

    try {
        await fetchJSON('/api/communities', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, description, model, posting_rate: rate, tone, style_notes: styleNotes }),
        });
        form.reset();
        document.getElementById('comm-tone').value = 'casual';
        await loadCommunities();
    } catch (err) {
        errorDiv.textContent = err.message;
    } finally {
        button.disabled = false;
        button.classList.remove('is-busy');
        button.textContent = 'Initialize community';
    }
}

document.addEventListener('DOMContentLoaded', () => {
    populateToneSelect();
    loadModels();
    loadCommunities();

    document.getElementById('new-community-form').addEventListener('submit', handleCreate);
    document.getElementById('edit-community-form').addEventListener('submit', handleEditSave);
    document.getElementById('comm-tone').addEventListener('change', () => updateTonePreview('comm-tone', 'comm-tone-preview'));
    document.getElementById('edit-community-tone').addEventListener('change', () => updateTonePreview('edit-community-tone', 'edit-tone-preview'));

    document.getElementById('communities-grid').addEventListener('click', (event) => {
        const editButton = event.target.closest('button[data-action="edit"]');
        if (editButton) {
            const card = editButton.closest('.community-card');
            const communityName = editButton.getAttribute('data-name');
            const community = window.__communities?.find((item) => item.name === communityName);
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

        const card = event.target.closest('.community-card[data-url]');
        if (card) {
            window.location.href = card.getAttribute('data-url');
        }
    });

    const modal = document.getElementById('delete-modal');
    modal.addEventListener('click', (event) => {
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

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && modal.classList.contains('is-open')) {
            closeDeleteModal();
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
        if (event.key === 'Escape' && editModal.classList.contains('is-open')) {
            closeEditModal();
        }
    });
});

