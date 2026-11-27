document.addEventListener('click', function (event) {
    const value = event.target.closest('.pcs-distance-value');
    if (!value || value.dataset.editing === 'true') return;
    edit(value);
});

document.addEventListener('keydown', function (event) {
    const value = event.target.closest('.pcs-distance-value');
    if (value && (event.key === 'Enter' || event.key === ' ')) {
        event.preventDefault();
        edit(value);
    }
});

function edit(target) {
    target.dataset.editing = 'true';
    const input = document.createElement('input');
    input.type = 'number';
    input.min = '0';
    input.max = '999999.9';
    input.step = '0.1';
    input.className = 'form-control form-control-sm d-inline-block';
    input.style.width = '7rem';
    input.value = target.dataset.distanceValue || '';
    target.replaceWith(input);
    input.focus();
    input.select();
    let finished = false;

    async function save() {
        if (finished) return;
        finished = true;
        const raw = input.value.trim().replace(',', '.');
        const number = Number(raw);
        if (!raw || !Number.isFinite(number) || number < 0 || Math.round(number * 10) !== number * 10) {
            target.dataset.editing = 'false';
            input.replaceWith(target);
            return;
        }
        input.disabled = true;
        try {
            const body = new URLSearchParams({
                csrf_token: target.dataset.csrfToken,
                stage_key: target.dataset.stageKey,
                distance_km: raw,
            });
            const response = await fetch(target.dataset.url, {
                method: 'POST', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: body.toString(),
            });
            const result = await response.json();
            if (!response.ok) throw new Error(result.error || 'Could not save distance');
            const formatted = Number(result.distance_km).toFixed(1);
            document.querySelectorAll('.pcs-distance-value').forEach(function (item) {
                if (item.dataset.raceId === target.dataset.raceId && item.dataset.stageKey === target.dataset.stageKey) {
                    item.dataset.distanceValue = formatted;
                    item.textContent = formatted;
                }
            });
            target.dataset.distanceValue = formatted;
            target.textContent = formatted;
            target.dataset.editing = 'false';
            input.replaceWith(target);
        } catch (error) {
            target.dataset.editing = 'false';
            input.replaceWith(target);
            window.alert(error.message);
        }
    }

    input.addEventListener('keydown', function (event) {
        if (event.key === 'Enter') { event.preventDefault(); save(); }
        if (event.key === 'Escape') {
            finished = true;
            target.dataset.editing = 'false';
            input.replaceWith(target);
        }
    });
    input.addEventListener('blur', save);
}
