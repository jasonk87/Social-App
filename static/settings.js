const aiSettingsState = { saved: null, profiles: [], signedIn: false, busy: false, thinking: true, activityBusy: false };
const aiElement = (id) => document.getElementById(id);

function selectedAIProfile() {
    return aiSettingsState.profiles.find(profile => profile.name === aiElement('ai-model').value);
}

function renderAIControls() {
    const profile = selectedAIProfile();
    const mode = profile?.thinking_mode;
    const disabled = !aiSettingsState.signedIn || aiSettingsState.busy || !aiSettingsState.saved;
    aiElement('ai-model').disabled = disabled || !aiSettingsState.profiles.length;
    aiElement('ai-save').disabled = disabled || !profile;
    aiElement('ai-refresh').disabled = aiSettingsState.busy;
    aiElement('ai-thinking').disabled = disabled || mode !== 'toggle';
    aiElement('ai-thinking').checked = mode === 'toggle' ? aiSettingsState.thinking : mode === 'levels';
    const help = {
        toggle: 'Let this model think before answering. Turn it off for quicker responses with less computation.',
        none: 'This model does not have a separate thinking mode.',
        levels: 'This model always uses thinking. Ollama cannot turn it off; the app uses its medium setting.',
        unknown: 'This Ollama version does not report thinking controls for this model. Its default behavior will be used.',
    };
    aiElement('ai-thinking-help').textContent = help[mode] || 'Choose an installed model to see its thinking options.';
    aiElement('ai-model-tag').textContent = profile ? `Ollama tag: ${profile.name}` : '';
}

function renderSavedAISettings() {
    const settings = aiSettingsState.saved;
    if (!settings) return;
    const profile = aiSettingsState.profiles.find(item => item.name === settings.model);
    aiElement('ai-current').textContent = `Current model: ${profile?.display_name || settings.model}`;
}

async function refreshAIModels() {
    const selected = aiElement('ai-model').value || aiSettingsState.saved?.model;
    aiSettingsState.busy = true;
    aiElement('ai-error').textContent = '';
    aiElement('ai-model-help').textContent = 'Checking Ollama…';
    renderAIControls();
    try {
        const data = await fetchJSON('/api/models');
        aiSettingsState.profiles = data.model_details || [];
        const select = aiElement('ai-model');
        select.replaceChildren();
        for (const profile of aiSettingsState.profiles) {
            select.add(new Option(profile.display_name || profile.name, profile.name));
        }
        if (selected && !aiSettingsState.profiles.some(profile => profile.name === selected)) {
            const missing = new Option(`${selected} (not installed)`, selected);
            missing.disabled = true;
            select.add(missing);
        }
        if (!select.options.length) select.add(new Option('No text models installed', ''));
        if (selected) select.value = selected;
        aiElement('ai-model-help').textContent = aiSettingsState.profiles.length
            ? `${aiSettingsState.profiles.length} installed text models. Refresh after adding or removing models in Ollama.`
            : 'No text models found. Install a model in Ollama, then refresh.';
    } catch (error) {
        aiSettingsState.profiles = [];
        aiElement('ai-model-help').textContent = 'Ollama is unavailable. Your saved model is unchanged.';
        aiElement('ai-error').textContent = error.message;
    } finally {
        aiSettingsState.busy = false;
        renderSavedAISettings();
        renderAIControls();
    }
}

async function saveAISettings(event) {
    event.preventDefault();
    const profile = selectedAIProfile();
    if (!profile || aiSettingsState.busy || !aiSettingsState.signedIn) return;
    aiSettingsState.busy = true;
    aiElement('ai-error').textContent = '';
    aiElement('ai-save-status').textContent = 'Saving…';
    renderAIControls();
    try {
        const data = await fetchJSON('/api/ai-settings', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model: profile.name,
                thinking_enabled: ['levels', 'unknown'].includes(profile.thinking_mode) ? true : aiSettingsState.thinking }),
        });
        Object.assign(aiSettingsState.saved, { model: data.settings.model,
            thinking_enabled: data.settings.thinking_enabled, thinking_mode: data.settings.thinking_mode });
        aiSettingsState.thinking = data.settings.thinking_enabled;
        renderSavedAISettings();
        aiElement('ai-save-status').textContent = 'Saved. Every community and bot now uses these settings.';
    } catch (error) {
        aiElement('ai-save-status').textContent = '';
        aiElement('ai-error').textContent = error.message;
    } finally {
        aiSettingsState.busy = false;
        renderAIControls();
    }
}

function renderActivitySettings() {
    const seconds = aiSettingsState.saved?.activity_interval_seconds ?? 60;
    const labels = { 0: 'Paused', 30: 'Every 30 seconds', 60: 'Every minute', 300: 'Every 5 minutes', 900: 'Every 15 minutes', 1800: 'Every 30 minutes' };
    aiElement('activity-current').textContent = `Current pace: ${labels[seconds]}`;
    const option = document.querySelector(`input[name="activity"][value="${seconds}"]`);
    if (option) option.checked = true;
    aiElement('activity-options').disabled = !aiSettingsState.signedIn || aiSettingsState.activityBusy;
    aiElement('activity-save').disabled = !aiSettingsState.signedIn || aiSettingsState.activityBusy;
}

async function saveActivitySettings(event) {
    event.preventDefault();
    if (!aiSettingsState.signedIn || aiSettingsState.activityBusy) return;
    const selected = document.querySelector('input[name="activity"]:checked');
    if (!selected) return;
    const interval = Number(selected.value);
    aiSettingsState.activityBusy = true;
    aiElement('activity-options').disabled = true;
    aiElement('activity-save').disabled = true;
    aiElement('activity-error').textContent = '';
    aiElement('activity-save-status').textContent = 'Saving…';
    try {
        const data = await fetchJSON('/api/activity-settings', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ activity_interval_seconds: interval }),
        });
        aiSettingsState.saved.activity_interval_seconds = data.settings.activity_interval_seconds;
        aiElement('activity-save-status').textContent = interval === 0
            ? 'Bots paused. You can still browse and post.'
            : 'Saved. All bots now share this activity pace.';
        renderActivitySettings();
    } catch (error) {
        aiElement('activity-save-status').textContent = '';
        aiElement('activity-error').textContent = error.message;
    } finally {
        aiSettingsState.activityBusy = false;
        aiElement('activity-options').disabled = false;
        aiElement('activity-save').disabled = false;
    }
}

document.addEventListener('DOMContentLoaded', async () => {
    if (window.lucide) window.lucide.createIcons();
    aiElement('activity-form').addEventListener('submit', saveActivitySettings);
    aiElement('activity-options').addEventListener('change', () => {
        aiElement('activity-save-status').textContent = '';
        aiElement('activity-error').textContent = '';
    });
    aiElement('ai-settings-form').addEventListener('submit', saveAISettings);
    aiElement('ai-refresh').addEventListener('click', refreshAIModels);
    aiElement('ai-model').addEventListener('change', () => {
        aiElement('ai-save-status').textContent = '';
        aiElement('ai-error').textContent = '';
        renderAIControls();
    });
    aiElement('ai-thinking').addEventListener('change', (event) => {
        aiSettingsState.thinking = event.target.checked;
        aiElement('ai-save-status').textContent = '';
    });
    try {
        const [session, data] = await Promise.all([fetchJSON('/api/session'), fetchJSON('/api/ai-settings')]);
        aiSettingsState.signedIn = !!session.current_user;
        aiSettingsState.saved = data.settings;
        aiSettingsState.thinking = data.settings.thinking_enabled;
        aiElement('ai-sign-in').hidden = aiSettingsState.signedIn;
        renderSavedAISettings();
        renderActivitySettings();
        await refreshAIModels();
    } catch (error) {
        aiElement('ai-current').textContent = 'Unable to load settings. Reload this page to retry.';
        aiElement('ai-error').textContent = error.message;
    }
});
