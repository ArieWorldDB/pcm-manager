(function () {
    'use strict';

    function selectedOptions(select) {
        return Array.from(select.options).filter(option => option.selected);
    }

    function buildOptions(select, optionsContainer, emptyMessage) {
        optionsContainer.replaceChildren();
        Array.from(select.options).forEach(function (option, index) {
            const label = document.createElement('label');
            label.className = 'multi-select-option';
            label.dataset.searchText = option.textContent.trim().toLocaleLowerCase();

            const checkbox = document.createElement('input');
            checkbox.type = 'checkbox';
            checkbox.value = option.value;
            checkbox.checked = option.selected;
            checkbox.dataset.optionIndex = String(index);
            checkbox.addEventListener('change', function () {
                const sourceOption = select.options[Number(checkbox.dataset.optionIndex)];
                if (sourceOption) sourceOption.selected = checkbox.checked;
                select.dispatchEvent(new Event('change', { bubbles: true }));
            });

            label.appendChild(checkbox);
            const profileIcon = profileIconUrl(select, option.textContent);
            if (profileIcon) {
                const image = document.createElement('img');
                image.className = 'multi-select-profile-icon';
                image.src = profileIcon;
                image.alt = '';
                image.setAttribute('aria-hidden', 'true');
                label.appendChild(image);
            }

            const text = document.createElement('span');
            text.textContent = option.textContent.trim();
            label.appendChild(text);
            optionsContainer.appendChild(label);
        });

        emptyMessage.hidden = select.options.length > 0;
    }

    function profileIconUrl(select, label) {
        if (select.id !== 'profile_values') return '';
        const value = String(label || '').trim().toLocaleLowerCase();
        if (value.includes('cobble')) return select.dataset.iconCobbles || '';
        if (value.includes('mountain')) {
            return value.includes('uphill')
                ? select.dataset.iconMountainsUphill || ''
                : select.dataset.iconMountains || '';
        }
        if (value.includes('hill')) {
            return value.includes('uphill')
                ? select.dataset.iconHillsUphill || ''
                : select.dataset.iconHills || '';
        }
        if (value.includes('flat')) return select.dataset.iconFlat || '';
        return '';
    }

    function syncOptionChecks(select, optionsContainer) {
        optionsContainer.querySelectorAll('input[type="checkbox"]').forEach(function (checkbox) {
            const option = select.options[Number(checkbox.dataset.optionIndex)];
            checkbox.checked = Boolean(option && option.selected);
        });
    }

    function initializeMultiSelect(select) {
        if (select.dataset.multiSelectInitialized === 'true') return;
        select.dataset.multiSelectInitialized = 'true';

        const originalLabel = document.querySelector(`label[for="${CSS.escape(select.id)}"]`);
        const labelText = originalLabel?.textContent.trim() || 'Options';
        select.dataset.filterLabel = labelText;
        if (originalLabel && !originalLabel.id) {
            originalLabel.id = `${select.id}-label`;
        }

        const widget = document.createElement('div');
        widget.className = 'multi-select-widget';
        const modeSelect = select.nextElementSibling instanceof HTMLSelectElement
            && select.nextElementSibling.classList.contains('filter-mode')
            ? select.nextElementSibling
            : null;

        const trigger = document.createElement('button');
        trigger.type = 'button';
        trigger.className = 'multi-select-trigger';
        trigger.id = `${select.id}-picker`;
        trigger.dataset.multiSelectFor = select.id;
        trigger.setAttribute('aria-expanded', 'false');
        trigger.setAttribute('aria-controls', `${select.id}-panel`);
        if (originalLabel) {
            trigger.setAttribute('aria-labelledby', originalLabel.id);
            originalLabel.removeAttribute('for');
        } else {
            trigger.setAttribute('aria-label', labelText);
        }

        const value = document.createElement('span');
        value.className = 'multi-select-value';
        value.id = `${select.id}-picker-value`;
        trigger.appendChild(value);
        if (originalLabel) {
            trigger.setAttribute('aria-labelledby', `${originalLabel.id} ${value.id}`);
        }

        const panel = document.createElement('div');
        panel.className = 'multi-select-panel';
        panel.id = `${select.id}-panel`;
        panel.hidden = true;

        const search = document.createElement('input');
        search.type = 'search';
        search.className = 'form-control form-control-sm multi-select-search';
        search.placeholder = 'Search options';
        search.setAttribute('aria-label', `Search ${labelText.toLocaleLowerCase()} options`);
        search.hidden = select.options.length < 8;
        const searchWrap = document.createElement('div');
        searchWrap.className = 'multi-select-search-wrap';
        const searchIcon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        searchIcon.setAttribute('viewBox', '0 0 24 24');
        searchIcon.setAttribute('fill', 'none');
        searchIcon.setAttribute('stroke', 'currentColor');
        searchIcon.setAttribute('stroke-width', '1.75');
        searchIcon.setAttribute('stroke-linecap', 'round');
        searchIcon.setAttribute('stroke-linejoin', 'round');
        searchIcon.setAttribute('aria-hidden', 'true');
        searchIcon.classList.add('filter-icon');
        const searchCircle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        searchCircle.setAttribute('cx', '10.8');
        searchCircle.setAttribute('cy', '10.8');
        searchCircle.setAttribute('r', '6.8');
        const searchHandle = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        searchHandle.setAttribute('d', 'm16 16 5 5');
        searchIcon.append(searchCircle, searchHandle);
        searchWrap.append(searchIcon, search);
        searchWrap.hidden = search.hidden;

        const optionsContainer = document.createElement('div');
        optionsContainer.className = 'multi-select-options';
        optionsContainer.setAttribute('role', 'group');
        optionsContainer.setAttribute('aria-label', labelText);

        const emptyMessage = document.createElement('div');
        emptyMessage.className = 'multi-select-empty';
        emptyMessage.textContent = 'No options available.';

        const noMatches = document.createElement('div');
        noMatches.className = 'multi-select-empty';
        noMatches.textContent = 'No matching options.';
        noMatches.hidden = true;

        const clearRow = document.createElement('div');
        clearRow.className = 'multi-select-clear';
        clearRow.hidden = select.selectedOptions.length === 0;
        const clearButton = document.createElement('button');
        clearButton.type = 'button';
        clearButton.className = 'btn btn-sm btn-link p-0';
        clearButton.textContent = 'Clear selection';
        clearButton.addEventListener('click', function () {
            Array.from(select.options).forEach(option => { option.selected = false; });
            if (modeSelect) modeSelect.value = 'include';
            select.dispatchEvent(new Event('change', { bubbles: true }));
            renderValue();
            buildOptions(select, optionsContainer, emptyMessage);
            updateModeControls();
            search.value = '';
            applySearch();
            trigger.focus();
        });
        clearRow.appendChild(clearButton);

        const modeControls = document.createElement('div');
        modeControls.className = 'multi-select-mode';
        modeControls.hidden = !modeSelect;
        if (modeSelect) {
            const modeLabel = document.createElement('span');
            modeLabel.className = 'multi-select-mode-label';
            modeLabel.textContent = 'Match selected values';
            const modeOptions = document.createElement('div');
            modeOptions.className = 'multi-select-mode-options';
            modeOptions.setAttribute('role', 'group');
            modeOptions.setAttribute('aria-label', `${labelText}: include or exclude`);
            [['include', 'Include'], ['exclude', 'Exclude']].forEach(function ([mode, text]) {
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'multi-select-mode-button';
                button.dataset.mode = mode;
                button.textContent = text;
                button.setAttribute('aria-pressed', String(modeSelect.value === mode));
                button.addEventListener('click', function () {
                    modeSelect.value = mode;
                    modeSelect.dispatchEvent(new Event('change', { bubbles: true }));
                    updateModeControls();
                    renderValue();
                });
                modeOptions.appendChild(button);
            });
            modeControls.append(modeLabel, modeOptions);
        }

        panel.append(searchWrap, optionsContainer, emptyMessage, noMatches, clearRow, modeControls);
        widget.append(trigger, panel);
        select.before(widget);
        select.classList.add('visually-hidden');
        select.tabIndex = -1;
        select.setAttribute('aria-hidden', 'true');
        if (modeSelect) {
            modeSelect.classList.add('visually-hidden');
            modeSelect.tabIndex = -1;
            modeSelect.setAttribute('aria-hidden', 'true');
        }

        function renderValue() {
            const selected = selectedOptions(select);
            if (modeSelect && selected.length === 0) modeSelect.value = 'include';
            if (selected.length === 0) {
                value.textContent = 'Any';
                trigger.title = `${labelText}: any`;
            } else if (selected.length === 1) {
                value.textContent = selected[0].textContent.trim();
                trigger.title = `${labelText}: ${value.textContent}`;
            } else {
                value.textContent = `${selected.length} selected`;
                trigger.title = `${labelText}: ${selected.map(option => option.textContent.trim()).join(', ')}`;
            }
            if (selected.length && modeSelect?.value === 'exclude') {
                value.textContent += ' (excluded)';
                trigger.title = `${labelText} excludes ${selected.map(option => option.textContent.trim()).join(', ')}`;
            }
            clearRow.hidden = selected.length === 0;
            modeControls.hidden = !modeSelect || selected.length === 0;
            updateModeControls();
        }

        function updateModeControls() {
            if (!modeSelect) return;
            modeControls.querySelectorAll('button[data-mode]').forEach(function (button) {
                button.setAttribute('aria-pressed', String(button.dataset.mode === modeSelect.value));
            });
        }

        function applySearch() {
            const query = search.value.trim().toLocaleLowerCase();
            let visibleCount = 0;
            optionsContainer.querySelectorAll('.multi-select-option').forEach(function (optionLabel) {
                const matches = !query || optionLabel.dataset.searchText.includes(query);
                optionLabel.hidden = !matches;
                if (matches) visibleCount += 1;
            });
            noMatches.hidden = visibleCount > 0 || select.options.length === 0;
        }

        function setOpen(open, focusSearch) {
            panel.hidden = !open;
            trigger.setAttribute('aria-expanded', String(open));
            widget.classList.toggle('is-open', open);
            if (open && focusSearch) {
                const triggerBounds = trigger.getBoundingClientRect();
                const estimatedPanelWidth = Math.min(
                    Math.max(triggerBounds.width, 272),
                    window.innerWidth - 32,
                );
                panel.style.left = triggerBounds.left + estimatedPanelWidth > window.innerWidth - 16
                    ? 'auto'
                    : '0';
                panel.style.right = triggerBounds.left + estimatedPanelWidth > window.innerWidth - 16
                    ? '0'
                    : 'auto';
                window.requestAnimationFrame(function () {
                    (search.hidden ? optionsContainer.querySelector('input[type="checkbox"]') : search)?.focus();
                });
            }
        }

        trigger.addEventListener('click', function () {
            setOpen(panel.hidden, true);
        });
        search.addEventListener('input', applySearch);
        panel.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') {
                event.preventDefault();
                setOpen(false, false);
                trigger.focus();
            }
        });
        document.addEventListener('click', function (event) {
            if (!widget.contains(event.target)) setOpen(false, false);
        });
        select.addEventListener('change', function () {
            renderValue();
            syncOptionChecks(select, optionsContainer);
            applySearch();
        });
        modeSelect?.addEventListener('change', function () {
            updateModeControls();
            renderValue();
        });

        const optionsObserver = new MutationObserver(function () {
            search.hidden = select.options.length < 8;
            searchWrap.hidden = search.hidden;
            buildOptions(select, optionsContainer, emptyMessage);
            renderValue();
            applySearch();
        });
        optionsObserver.observe(select, { childList: true });

        buildOptions(select, optionsContainer, emptyMessage);
        renderValue();
    }

    function initialize() {
        document.querySelectorAll('#race-filters-form select[multiple][id]').forEach(initializeMultiSelect);
        initializeSelectedFilterSummary();
    }

    function initializeSelectedFilterSummary() {
        const form = document.getElementById('race-filters-form');
        const summary = document.getElementById('selected-filter-summary');
        const chips = document.getElementById('selected-filter-chips');
        const moreFiltersCount = document.getElementById('more-filters-count');
        if (!form || !summary || !chips) return;

        const filterNames = {
            race_year_values: 'Year',
            nation_values: 'Country',
            classification_values: 'Category',
            month_values: 'Month',
            profile_values: 'Route profile',
            source_values: 'Source',
            race_type_values: 'Race type',
        };
        const scalarFilters = [
            ['race_name', 'Race name'],
            ['junior_race', 'Junior'],
            ['has_time_trial', 'Contains TT'],
            ['start_date_from', 'Start date from'],
            ['start_date_to', 'Start date to'],
            ['distance_min', 'Minimum stage distance (km)'],
            ['distance_max', 'Maximum stage distance (km)'],
        ];
        const statusLabels = {
            waiting_edits: 'Waiting Edits',
            pending_approval: 'Waiting Approval',
            approved: 'Approved',
        };

        function removeChip(chip) {
            const { controlName, controlValue, isMulti, isStatus } = chip.dataset;
            if (isMulti) {
                const select = form.elements.namedItem(controlName);
                const option = Array.from(select.options).find(item => item.value === controlValue);
                if (option) option.selected = false;
                const modeName = controlName.replace('_values', '_mode');
                const mode = form.elements.namedItem(modeName);
                if (mode && !Array.from(select.selectedOptions).length) mode.value = 'include';
                select.dispatchEvent(new Event('change', { bubbles: true }));
            } else if (isStatus) {
                const checkbox = Array.from(form.querySelectorAll('input[name="edit_status"]'))
                    .find(item => item.value === controlValue);
                if (checkbox) {
                    checkbox.checked = false;
                    checkbox.dispatchEvent(new Event('change', { bubbles: true }));
                }
            } else {
                const control = form.elements.namedItem(controlName);
                if (control) {
                    control.value = '';
                    control.dispatchEvent(new Event('input', { bubbles: true }));
                    control.dispatchEvent(new Event('change', { bubbles: true }));
                }
            }
            renderChips();
        }

        function addChip(label, controlName, controlValue, options = {}) {
            const chip = document.createElement('span');
            chip.className = 'selected-filter-chip';
            chip.dataset.controlName = controlName;
            chip.dataset.controlValue = controlValue || '';
            if (options.isMulti) chip.dataset.isMulti = 'true';
            if (options.isStatus) chip.dataset.isStatus = 'true';

            const text = document.createElement('span');
            text.className = 'selected-filter-chip-label';
            text.textContent = label;

            const remove = document.createElement('button');
            remove.type = 'button';
            remove.className = 'selected-filter-chip-remove';
            remove.setAttribute('aria-label', `Remove ${label} filter`);
            remove.textContent = '\u00D7';
            remove.addEventListener('click', function () { removeChip(chip); });

            chip.append(text, remove);
            chips.appendChild(chip);
        }

        function renderChips() {
            chips.replaceChildren();

            Object.entries(filterNames).forEach(function ([name, label]) {
                const select = form.elements.namedItem(name);
                const selected = Array.from(select.selectedOptions);
                if (!selected.length) return;
                const mode = form.elements.namedItem(name.replace('_values', '_mode'))?.value;
                const prefix = mode === 'exclude' ? `${label} excludes` : label;
                selected.forEach(option => addChip(`${prefix}: ${option.textContent.trim()}`, name, option.value, { isMulti: true }));
            });

            scalarFilters.forEach(function ([name, label]) {
                const control = form.elements.namedItem(name);
                if (!control || !String(control.value || '').trim()) return;
                let value = control.value;
                if (name === 'junior_race') value = value === 'true' ? 'Yes' : 'No';
                if (name === 'has_time_trial') value = value === 'true' ? 'Yes' : 'No';
                addChip(`${label}: ${value}`, name, value);
            });

            Array.from(form.querySelectorAll('input[name="edit_status"]:checked')).forEach(function (checkbox) {
                addChip(statusLabels[checkbox.value] || checkbox.value, 'edit_status', checkbox.value, { isStatus: true });
            });

            summary.hidden = chips.childElementCount === 0;
            renderMoreFiltersCount();
        }

        function renderMoreFiltersCount() {
            if (!moreFiltersCount) return;
            const activeGroups = [
                Array.from(form.elements.namedItem('month_values').selectedOptions).length > 0,
                Array.from(form.elements.namedItem('profile_values').selectedOptions).length > 0,
                Array.from(form.elements.namedItem('source_values').selectedOptions).length > 0,
                Array.from(form.elements.namedItem('race_type_values').selectedOptions).length > 0,
                Boolean(form.elements.namedItem('junior_race').value),
                Boolean(form.elements.namedItem('has_time_trial').value),
                Boolean(form.elements.namedItem('distance_min').value || form.elements.namedItem('distance_max').value),
                Boolean(form.elements.namedItem('start_date_from').value || form.elements.namedItem('start_date_to').value),
            ];
            const count = activeGroups.filter(Boolean).length;
            moreFiltersCount.textContent = `${count} active`;
            moreFiltersCount.hidden = count === 0;
        }

        form.addEventListener('input', renderChips);
        form.addEventListener('change', renderChips);
        renderChips();
    }

    initialize();
})();
