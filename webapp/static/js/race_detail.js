document.addEventListener('DOMContentLoaded', function () {
    const config = window.raceDetailConfig || {};
    const prefix = 'results';
    const form = document.getElementById('save-form');
    const saveButton = document.getElementById('save-button');
    const changesInput = document.getElementById('changes-json');
    const saveHint = document.getElementById('save-hint');
    const conflictChoices = new Map();
    const resolvedConflicts = new Set();
    const rangeClasses = ['range-low', 'range-decent', 'range-good', 'range-very-good', 'range-excellent', 'range-elite'];
    const timeSeparatorsPreferenceKey = 'race-results-time-separators';
    let stickyRaceHeaderCleanup = null;

    function initializeStickyRaceHeader() {
        if (stickyRaceHeaderCleanup) stickyRaceHeaderCleanup();
        const wrapper = document.querySelector('.results-table-wrapper');
        const table = wrapper && wrapper.querySelector('.results-table');
        const originalHead = table && table.querySelector('thead');
        const statsSection = table && table.closest('#stage-results');
        if (!wrapper || !table || !originalHead) return;

        const floatingHeader = document.createElement('div');
        floatingHeader.className = 'race-table-sticky-header';
        floatingHeader.setAttribute('aria-hidden', 'true');
        const floatingTable = table.cloneNode(false);
        floatingTable.removeAttribute('id');
        floatingTable.style.width = table.offsetWidth + 'px';
        const clonedHead = originalHead.cloneNode(true);
        const originalCells = originalHead.querySelectorAll('th');
        const clonedCells = clonedHead.querySelectorAll('th');
        originalCells.forEach(function (cell, index) {
            if (clonedCells[index]) clonedCells[index].style.width = cell.getBoundingClientRect().width + 'px';
        });
        floatingTable.appendChild(clonedHead);
        floatingHeader.appendChild(floatingTable);
        document.body.appendChild(floatingHeader);

        function update() {
            const wrapperRect = wrapper.getBoundingClientRect();
            const tableRect = table.getBoundingClientRect();
            const sectionRect = (statsSection || wrapper).getBoundingClientRect();
            const headHeight = originalHead.getBoundingClientRect().height;
            floatingHeader.style.left = wrapperRect.left - wrapper.scrollLeft + 'px';
            floatingHeader.style.width = wrapper.clientWidth + 'px';
            floatingTable.style.width = table.offsetWidth + 'px';
            floatingTable.style.transform = 'translateX(' + (-wrapper.scrollLeft) + 'px)';
            originalCells.forEach(function (cell, index) {
                if (clonedCells[index]) clonedCells[index].style.width = cell.getBoundingClientRect().width + 'px';
            });
            const shouldShow = sectionRect.top < 0 && sectionRect.bottom > headHeight && tableRect.bottom > headHeight;
            floatingHeader.hidden = !shouldShow;
        }

        function forwardSort(event) {
            const floatingCell = event.target.closest('th');
            if (!floatingCell) return;
            const cells = Array.from(floatingCell.parentElement.children);
            const index = cells.indexOf(floatingCell);
            const originalCell = originalHead.querySelectorAll('th')[index];
            if (originalCell && originalCell.classList.contains('sortable-col')) {
                originalCell.click();
                originalHead.querySelectorAll('th').forEach(function (cell, cellIndex) {
                    const cloneCell = floatingTable.querySelectorAll('th')[cellIndex];
                    if (cloneCell) cloneCell.className = cell.className;
                });
            }
        }

        floatingHeader.addEventListener('click', forwardSort);
        window.addEventListener('scroll', update, { passive: true });
        window.addEventListener('resize', update);
        wrapper.addEventListener('scroll', update, { passive: true });
        update();
        stickyRaceHeaderCleanup = function () {
            window.removeEventListener('scroll', update);
            window.removeEventListener('resize', update);
            wrapper.removeEventListener('scroll', update);
            floatingHeader.remove();
            stickyRaceHeaderCleanup = null;
        };
    }

    function updateRaceChangesDownload(text, filename) {
        const button = document.querySelector('[data-download-race-changes]');
        if (!button) return;
        button.dataset.exportText = text || '';
        button.dataset.downloadFilename = filename || 'race-stat-changes.txt';
        button.dataset.exportCopied = 'false';
        button.textContent = 'Copy changes to clipboard';
        button.hidden = !text;
    }

    updateRaceChangesDownload(config.changeExportText, config.changeExportFilename);

    document.addEventListener('click', async function (event) {
        const button = event.target.closest('[data-download-race-changes]');
        if (!button || !button.dataset.exportText) return;
        if (button.dataset.exportCopied !== 'true') {
            try {
                if (navigator.clipboard && window.isSecureContext) {
                    await navigator.clipboard.writeText(button.dataset.exportText);
                } else {
                    const temporaryInput = document.createElement('textarea');
                    temporaryInput.value = button.dataset.exportText;
                    temporaryInput.setAttribute('readonly', '');
                    temporaryInput.style.position = 'fixed';
                    temporaryInput.style.opacity = '0';
                    document.body.appendChild(temporaryInput);
                    temporaryInput.select();
                    const copied = document.execCommand('copy');
                    temporaryInput.remove();
                    if (!copied) throw new Error('Clipboard copy failed');
                }
                button.dataset.exportCopied = 'true';
                button.textContent = 'Download changes (.txt)';
                return;
            } catch (error) {
                button.textContent = 'Copy failed — try again';
                return;
            }
        }
        const blob = new Blob([button.dataset.exportText], { type: 'text/plain;charset=utf-8' });
        const objectUrl = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = objectUrl;
        link.download = button.dataset.downloadFilename || 'race-stat-changes.txt';
        document.body.appendChild(link);
        link.click();
        link.remove();
        window.setTimeout(function () { URL.revokeObjectURL(objectUrl); }, 1000);
    });

    function statInputs() {
        return Array.from(document.querySelectorAll('.stat-input[data-source-rider-id]'));
    }

    function inputFor(riderId, statName) {
        return document.querySelector('.stat-input[data-source-rider-id="' + riderId + '"][data-stat-name="' + statName + '"]');
    }

    function applyStatColors() {
        statInputs().forEach(function (input) {
            input.classList.remove(...rangeClasses);
            if (input.dataset.statName === 'potential') return;
            const value = Number.parseInt(input.value, 10);
            if (!Number.isFinite(value)) return;
            input.classList.add(value <= 65 ? 'range-low' : value <= 69 ? 'range-decent' : value <= 73 ? 'range-good' : value <= 77 ? 'range-very-good' : value <= 81 ? 'range-excellent' : 'range-elite');
        });
    }

    function updateDelta(input) {
        const original = Number.parseFloat(input.dataset.originalValue);
        const current = Number.parseFloat(input.value);
        const delta = input.parentElement.querySelector('.delta-display');
        if (!delta || !Number.isFinite(original) || !Number.isFinite(current)) return;
        const row = input.closest('.stat-input-row');
        const valueLength = input.value.trim().length;
        const isPotential = input.dataset.statName === 'potential';
        input.style.setProperty('--stat-font-large', isPotential ? '.95rem' : valueLength > 2 ? '.8rem' : valueLength === 2 ? '1.08rem' : '1.3rem');
        input.style.setProperty('--stat-font-small', isPotential ? '.68rem' : valueLength > 2 ? '.68rem' : valueLength === 2 ? '.88rem' : '1.08rem');
        const amount = current - original;
        delta.hidden = amount === 0;
        const displayedAmount = input.dataset.statName === 'potential'
            ? (Number.isInteger(amount) ? String(amount) : amount.toFixed(1).replace('.', ','))
            : String(amount);
        delta.textContent = (amount > 0 ? '+' : '') + displayedAmount;
        const deltaLength = delta.textContent.length;
        delta.style.setProperty('--delta-font-large', isPotential ? '.78rem' : deltaLength > 3 ? '.7rem' : deltaLength === 3 ? '.9rem' : '1rem');
        delta.style.setProperty('--delta-font-small', isPotential ? '.46rem' : deltaLength > 3 ? '.44rem' : deltaLength === 3 ? '.56rem' : '.82rem');
        delta.style.color = amount > 0 ? '#198754' : '#dc3545';
        row.classList.toggle('has-stat-delta', amount !== 0);
    }

    function applyFilters() {
        const hadEditsFilter = document.getElementById(prefix + '-had-edits');
        const teamFilter = document.getElementById(prefix + '-team-filter');
        const emptyFilterMessage = document.getElementById(prefix + '-filter-empty');
        const separatorsToggle = document.getElementById(prefix + '-time-separators');
        const rows = Array.from(document.querySelectorAll('.results-table tbody tr'));
        let visibleCount = 0;
        rows.forEach(function (row) {
            const hasEdits = Array.from(row.querySelectorAll('.stat-input')).some(function (input) {
                return input.value !== input.dataset.original;
            });
            row.hidden = Boolean((hadEditsFilter && hadEditsFilter.checked && !hasEdits) || (teamFilter && teamFilter.value && row.dataset.filterTeam !== teamFilter.value));
            if (!row.hidden) visibleCount += 1;
        });
        if (emptyFilterMessage) emptyFilterMessage.hidden = visibleCount > 0;
        const table = document.querySelector('.results-table');
        const positionHeader = table && table.querySelector('thead th:first-child');
        const positionSorted = Boolean(positionHeader && (positionHeader.classList.contains('sort-asc') || positionHeader.classList.contains('sort-desc')));
        if (table) updateSameTimeSeparators(table, positionSorted
            && table.dataset.timeSeparatorsAllowed !== 'false'
            && Boolean(separatorsToggle && separatorsToggle.checked));
    }

    function updateSameTimeSeparators(table, positionSorted) {
        if (!table || !table.tBodies[0]) return;
        table.classList.toggle('position-sorted', positionSorted);
        const visibleRows = Array.from(table.tBodies[0].rows).filter(row => !row.hidden);
        function timeSeconds(row) {
            if (row.cells[0] && row.cells[0].dataset.sortValue === '1') return 0;
            const value = (row.dataset.resultTime || '').trim();
            let match = value.match(/^(\d+)\.(\d{2})$/);
            if (match) return Number(match[1]) * 60 + Number(match[2]);
            match = value.match(/^(\d+):(\d{2})(?::(\d{2}))?$/);
            if (!match) return null;
            return match[3] === undefined
                ? Number(match[1]) * 60 + Number(match[2])
                : Number(match[1]) * 3600 + Number(match[2]) * 60 + Number(match[3]);
        }
        visibleRows.forEach(function (row, index) {
            const nextRow = visibleRows[index + 1];
            const currentSeconds = timeSeconds(row);
            const nextSeconds = nextRow ? timeSeconds(nextRow) : null;
            row.classList.toggle(
                'same-time-group-end',
                Boolean(positionSorted && nextRow && currentSeconds !== null && nextSeconds !== null
                    && Math.abs(nextSeconds - currentSeconds) > 2),
            );
        });
    }

    function updateSeparatorsToggleAvailability(table, positionHeader, separatorsToggle) {
        if (!table) return;
        const positionSorted = table.dataset.timeSeparatorsAllowed !== 'false' && Boolean(positionHeader && (
            positionHeader.classList.contains('sort-asc') || positionHeader.classList.contains('sort-desc')
        ));
        if (separatorsToggle) separatorsToggle.disabled = !positionSorted;
        updateSameTimeSeparators(table, positionSorted && Boolean(separatorsToggle && separatorsToggle.checked));
    }

    function collectChanges() {
        const changes = new Map();
        statInputs().forEach(function (input) {
            const riderId = Number.parseInt(input.dataset.sourceRiderId, 10);
            const statName = input.dataset.statName;
            const finalValue = Number.parseFloat(input.value);
            const key = riderId + ':' + statName;
            const override = conflictChoices.get(key) === 'proposed';
            if (!Number.isFinite(riderId) || !Number.isFinite(finalValue) || (!resolvedConflicts.has(key) && input.value === input.dataset.savedValue)) return;
            const change = { stat_name: statName, final_value: finalValue, is_confilict_override: override };
            change[config.riderIdField] = riderId;
            if (config.includeOriginalValue) change.original_value = Number.parseFloat(input.dataset.originalValue);
            changes.set(key, change);
        });
        return Array.from(changes.values());
    }

    function refresh() {
        const changes = collectChanges();
        if (changesInput) changesInput.value = JSON.stringify(changes);
        if (saveButton) saveButton.disabled = !config.canUpdateStats || changes.length === 0;
        if (saveHint) saveHint.textContent = changes.length ? changes.length + ' change(s) pending.' : 'No changes yet.';
        applyStatColors();
        applyFilters();
    }

    function initializeTable() {
        const hadEditsFilter = document.getElementById(prefix + '-had-edits');
        const teamFilter = document.getElementById(prefix + '-team-filter');
        const separatorsToggle = document.getElementById(prefix + '-time-separators');
        if (!teamFilter) return;
        if (separatorsToggle) {
            const savedPreference = window.localStorage.getItem(timeSeparatorsPreferenceKey);
            separatorsToggle.checked = savedPreference === null || savedPreference === 'true';
            separatorsToggle.addEventListener('change', function () {
                window.localStorage.setItem(timeSeparatorsPreferenceKey, String(separatorsToggle.checked));
                applyFilters();
            });
        }
        const teams = [...new Set(Array.from(document.querySelectorAll('.results-table tbody tr')).map(row => row.dataset.filterTeam).filter(Boolean))].sort((a, b) => a.localeCompare(b));
        teams.forEach(team => teamFilter.add(new Option(team, team)));
        teamFilter.addEventListener('change', applyFilters);
        if (hadEditsFilter) hadEditsFilter.addEventListener('change', applyFilters);

        const table = document.querySelector('.results-table');
        if (!table) return;
        const positionHeader = table.querySelector('thead th:first-child');
        const initiallyPositionSorted = Boolean(positionHeader && positionHeader.classList.contains('sort-asc'));
        updateSeparatorsToggleAvailability(table, positionHeader, separatorsToggle);
        const headers = Array.from(table.querySelectorAll('thead th.sortable-col'));
        headers.forEach(function (header) {
            header.addEventListener('click', function () {
                const body = table.tBodies[0];
                const direction = header.classList.contains('sort-asc') ? 'desc' : 'asc';
                headers.forEach(item => item.classList.remove('sort-asc', 'sort-desc'));
                header.classList.add('sort-' + direction);
                const numeric = header.dataset.sortType === 'number';
                Array.from(body.rows).sort(function (a, b) {
                    const firstCell = a.cells[header.cellIndex];
                    const secondCell = b.cells[header.cellIndex];
                    const first = (firstCell.querySelector('input')?.value || firstCell.dataset.sortValue || firstCell.textContent).trim();
                    const second = (secondCell.querySelector('input')?.value || secondCell.dataset.sortValue || secondCell.textContent).trim();
                    let compared;
                    if (numeric) {
                        const firstNumber = Number(first);
                        const secondNumber = Number(second);
                        const firstValue = Number.isFinite(firstNumber) ? firstNumber : Infinity;
                        const secondValue = Number.isFinite(secondNumber) ? secondNumber : Infinity;
                        compared = firstValue === secondValue ? 0 : firstValue < secondValue ? -1 : 1;
                    } else compared = first.localeCompare(second);
                    return direction === 'asc' ? compared : -compared;
                }).forEach(row => body.appendChild(row));
                updateSeparatorsToggleAvailability(table, positionHeader, separatorsToggle);
            });
        });
    }

    document.addEventListener('input', function (event) {
        if (!event.target.matches('.stat-input[data-source-rider-id]')) return;
        updateDelta(event.target);
        refresh();
    });
    document.addEventListener('change', function (event) {
        if (!event.target.matches('.stat-input[data-source-rider-id]')) return;
        const input = event.target;
        const value = Number(input.value);
        const minimum = Number(input.min);
        const maximum = Number(input.max);
        if (input.value === '' || !Number.isFinite(value) || value < minimum || value > maximum) {
            input.value = input.dataset.originalValue;
        }
        updateDelta(input);
        refresh();
    });
    document.addEventListener('click', function (event) {
        const choice = event.target.closest('.conflict-resolution-btn');
        if (choice) {
            const riderId = choice.dataset.sourceRiderId;
            const statName = choice.dataset.statName;
            const input = inputFor(riderId, statName);
            if (!input) return;
            input.value = choice.dataset.resolutionValue;
            conflictChoices.set(riderId + ':' + statName, choice.dataset.resolution);
            resolvedConflicts.add(riderId + ':' + statName);
            updateDelta(input);
            refresh();
            return;
        }
    });
    if (form) form.addEventListener('submit', function (event) {
        refresh();
        if (!changesInput.value || changesInput.value === '[]') event.preventDefault();
    });

    const resultsContainer = document.getElementById('race-results-container');
    if (resultsContainer && resultsContainer.dataset.resultsUrl) {
        fetch(resultsContainer.dataset.resultsUrl, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
            .then(response => { if (!response.ok) throw new Error('The race results could not be loaded.'); return response.text(); })
            .then(html => {
                resultsContainer.innerHTML = html;
                const changesExport = resultsContainer.querySelector('[data-race-change-export]');
                if (changesExport) {
                    updateRaceChangesDownload(changesExport.value, changesExport.dataset.downloadFilename);
                }
                if (window.initializeStatInputControls) window.initializeStatInputControls(resultsContainer);
                const metadata = resultsContainer.querySelector('[data-pcs-race-metadata]');
                if (metadata) {
                    if (metadata.dataset.resultCached === 'true') {
                        const title = document.querySelector('.race-page-header h2');
                        if (title && !title.querySelector('[data-pcs-result-downloaded]')) {
                            const icon = document.createElement('span');
                            icon.className = 'race-title-status';
                            icon.title = 'Race result downloaded';
                            icon.dataset.pcsResultDownloaded = 'true';
                            icon.textContent = '⬇️';
                            title.appendChild(document.createTextNode(' '));
                            title.appendChild(icon);
                        }
                    }
                    [['classification', 'race-classification'], ['nation', 'race-nation'], ['profile', 'race-profile'], ['distance', 'race-distance'], ['startLocation', 'race-from'], ['endLocation', 'race-to'], ['startDate', 'race-start-date']].forEach(function ([key, id]) {
                        const target = document.getElementById(id);
                        if (metadata.dataset[key] && target) target.textContent = metadata.dataset[key];
                    });
                }
                initializeTable();
                initializeStickyRaceHeader();
                statInputs().forEach(updateDelta);
                refresh();
            })
            .catch(error => { resultsContainer.innerHTML = '<div class="alert alert-warning mb-0" role="alert">' + error.message + '</div>'; });
    } else {
        initializeTable();
        initializeStickyRaceHeader();
        statInputs().forEach(updateDelta);
        refresh();
    }

    document.querySelectorAll('.get-result-form').forEach(function (getResultForm) {
        getResultForm.addEventListener('submit', function () {
            const button = getResultForm.querySelector('.get-result-submit');
            if (button && !button.disabled) { button.disabled = true; button.textContent = 'Saving…'; }
        });
    });
});
