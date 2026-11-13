window.initializeStatInputControls = function (root) {
    (root || document).querySelectorAll('.stat-input-row .stat-input[type="number"]').forEach(function (input) {
        if (input.dataset.stepControlsInitialized === 'true') {
            return;
        }
        const controls = document.createElement('div');
        controls.className = 'stat-step-controls';
        controls.addEventListener('pointermove', function (event) {
            const bounds = controls.getBoundingClientRect();
            controls.dataset.activeStep = event.clientY < bounds.top + bounds.height / 2 ? '1' : '-1';
        });
        controls.addEventListener('pointerleave', function () {
            delete controls.dataset.activeStep;
        });
        [1, -1].forEach(function (step) {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = step > 0 ? 'stat-step-up' : 'stat-step-down';
            button.textContent = step > 0 ? '+' : '−';
            button.setAttribute('aria-label', (step > 0 ? 'Increase ' : 'Decrease ') + input.dataset.statName);
            button.disabled = input.disabled;
            button.addEventListener('click', function (event) {
                if (input.disabled || input.readOnly) {
                    return;
                }
                let clickedStep = step;
                if (event.detail !== 0) {
                    const bounds = controls.getBoundingClientRect();
                    clickedStep = event.clientY < bounds.top + bounds.height / 2 ? 1 : -1;
                }
                if (clickedStep > 0) {
                    input.stepUp();
                } else {
                    input.stepDown();
                }
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
            });
            controls.appendChild(button);
        });
        input.parentElement.appendChild(controls);
        input.dataset.stepControlsInitialized = 'true';
    });
};

document.addEventListener('DOMContentLoaded', function () {
    window.initializeStatInputControls();
});
