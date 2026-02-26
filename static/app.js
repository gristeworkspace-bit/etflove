document.addEventListener('DOMContentLoaded', () => {
    const fetchDataBtn = document.getElementById('fetchDataBtn');
    const downloadCSVBtn = document.getElementById('downloadCSVBtn');
    const fetchAllToggle = document.getElementById('fetchAllToggle');
    const loadingStatus = document.getElementById('loadingStatus');
    const dataTable = document.getElementById('dataTable');
    const tableBody = document.getElementById('tableBody');
    const noDataMessage = document.getElementById('noDataMessage');
    const lastUpdated = document.getElementById('lastUpdated');
    const targetDateDisplay = document.getElementById('targetDateDisplay');
    const sortHeaders = document.querySelectorAll('.sortable');

    let currentData = [];
    let currentSort = { column: null, direction: 'asc' };

    fetchDataBtn.addEventListener('click', async () => {
        // UI Loading state
        fetchDataBtn.disabled = true;
        downloadCSVBtn.disabled = true;
        fetchAllToggle.disabled = true;
        loadingStatus.classList.remove('hidden');
        dataTable.classList.add('hidden');
        noDataMessage.classList.add('hidden');
        lastUpdated.classList.add('hidden');
        tableBody.innerHTML = '';

        const isFetchAll = fetchAllToggle.checked;
        const limitParam = isFetchAll ? 0 : 20;

        try {
            const response = await fetch(`/api/fetch_etfs?limit=${limitParam}`);
            const data = await response.json();

            if (data.status === 'success') {
                currentData = data.data;
                targetDateDisplay.textContent = data.target_date;
                renderTable(currentData);

                loadingStatus.classList.add('hidden');
                dataTable.classList.remove('hidden');
                lastUpdated.classList.remove('hidden');
                downloadCSVBtn.classList.remove('hidden');
                downloadCSVBtn.disabled = false;
            } else {
                loadingStatus.classList.add('hidden');
                noDataMessage.innerHTML = `<i class="fas fa-exclamation-triangle val-negative" style="font-size: 2rem; margin-bottom: 1rem;"></i><p class="val-negative">エラーが発生しました: ${data.error}</p>`;
                noDataMessage.classList.remove('hidden');
            }
        } catch (error) {
            console.error("Fetch error:", error);
            loadingStatus.classList.add('hidden');
            noDataMessage.innerHTML = `<i class="fas fa-exclamation-triangle val-negative" style="font-size: 2rem; margin-bottom: 1rem;"></i><p class="val-negative">通信エラーが発生しました。サーバーが動いているか確認してください。</p>`;
            noDataMessage.classList.remove('hidden');
        } finally {
            fetchDataBtn.disabled = false;
            fetchAllToggle.disabled = false;
        }
    });

    // Formatting helpers
    const formatNumber = (num, noDecimals = false) => {
        if (num === null || num === undefined) return '-';
        return new Intl.NumberFormat('ja-JP', {
            minimumFractionDigits: noDecimals ? 0 : 2,
            maximumFractionDigits: noDecimals ? 0 : 2
        }).format(num);
    };

    const formatPct = (num) => {
        if (num === null || num === undefined) return { text: '-', class: 'val-neutral' };
        const formatted = (num > 0 ? '+' : '') + num.toFixed(2) + '%';
        let cls = 'val-neutral';
        if (num > 0) cls = 'val-positive';
        else if (num < 0) cls = 'val-negative';
        return { text: formatted, class: cls };
    };

    function renderTable(dataToRender) {
        tableBody.innerHTML = '';

        dataToRender.forEach((row, index) => {
            const tr = document.createElement('tr');
            tr.className = 'fade-in';
            tr.style.animationDelay = `${Math.min(index * 0.05, 0.5)}s`;

            const pct1d = formatPct(row.change_1d_pct);
            const pct1w = formatPct(row.change_1w_pct);
            const pct2w = formatPct(row.change_2w_pct);
            const pct1y = formatPct(row.change_1y_pct);

            tr.innerHTML = `
                <td><span class="pill">${row.code}</span></td>
                <td class="td-name">${row.name}</td>
                <td class="td-benchmark">${row.benchmark}</td>
                <td class="td-management">${row.management}</td>
                <td>${row.fee}</td>
                <td class="align-right highlight-col"><strong>${row.price ? formatNumber(row.price) : '-'}</strong></td>
                <td class="align-right ${pct1d.class}">${pct1d.text}</td>
                <td class="align-right ${pct1w.class}">${pct1w.text}</td>
                <td class="align-right ${pct2w.class}">${pct2w.text}</td>
                <td class="align-right ${pct1y.class}">${pct1y.text}</td>
                <td class="align-right highlight-col-alt"><strong>${row.dividend_yield || '-'}</strong></td>
                <td class="align-center">${row.dividend_date || '-'}</td>
            `;
            tableBody.appendChild(tr);
        });
    }

    // Sorting Logic
    sortHeaders.forEach(header => {
        header.addEventListener('click', () => {
            const column = header.dataset.sort;

            // Toggle direction
            if (currentSort.column === column) {
                currentSort.direction = currentSort.direction === 'asc' ? 'desc' : 'asc';
            } else {
                currentSort.column = column;
                currentSort.direction = 'asc';
            }

            // Update icons
            sortHeaders.forEach(h => h.classList.remove('sort-asc', 'sort-desc'));
            header.classList.add(currentSort.direction === 'asc' ? 'sort-asc' : 'sort-desc');

            // Perform sort
            sortData(currentSort.column, currentSort.direction);
        });
    });

    function sortData(column, direction) {
        const sortedData = [...currentData].sort((a, b) => {
            let valA = a[column];
            let valB = b[column];

            // Handle nulls
            if (valA === null || valA === '-' || valA === undefined || valA === '""') valA = direction === 'asc' ? Infinity : -Infinity;
            if (valB === null || valB === '-' || valB === undefined || valB === '""') valB = direction === 'asc' ? Infinity : -Infinity;

            // Handle numeric fields by stripping formatting or converting
            if (['price', 'change_1d_pct', 'change_1w_pct', 'change_2w_pct', 'change_1y_pct', 'dividend_yield'].includes(column)) {
                const parseNum = (val) => {
                    if (typeof val === 'number') return val;
                    if (typeof val === 'string') {
                        // Strip '%' and ','
                        const clean = val.replace(/%|,/g, '');
                        const num = parseFloat(clean);
                        return isNaN(num) ? (direction === 'asc' ? Infinity : -Infinity) : num;
                    }
                    return direction === 'asc' ? Infinity : -Infinity;
                };
                valA = parseNum(valA);
                valB = parseNum(valB);

                if (valA < valB) return direction === 'asc' ? -1 : 1;
                if (valA > valB) return direction === 'asc' ? 1 : -1;
                return 0;
            }

            // Special handling for dividend date
            if (column === 'dividend_date') {
                const parseDate = (d) => {
                    if (d === Infinity || d === -Infinity) return d;
                    const str = String(d);
                    const m1 = str.match(/(\d{4})-(\d{2})-(\d{2})/);
                    if (m1) return new Date(m1[1], m1[2] - 1, m1[3]).getTime();
                    const m2 = str.match(/(\d{4})年(\d{1,2})月(\d{1,2})日/);
                    if (m2) return new Date(m2[1], m2[2] - 1, m2[3]).getTime();
                    return 0;
                };
                const timeA = parseDate(valA);
                const timeB = parseDate(valB);
                if (timeA < timeB) return direction === 'asc' ? -1 : 1;
                if (timeA > timeB) return direction === 'asc' ? 1 : -1;
                return 0;
            }

            // String comparison (for names, codes, benchmark, etc.)
            const strA = String(valA);
            const strB = String(valB);

            return direction === 'asc' ? strA.localeCompare(strB, 'ja') : strB.localeCompare(strA, 'ja');
        });

        renderTable(sortedData);
    }

    // CSV Download Logic
    downloadCSVBtn.addEventListener('click', () => {
        if (!currentData || currentData.length === 0) return;

        // Force Excel to read as UTF-8 by including BOM
        const BOM = '\uFEFF';

        // Headers
        const headers = [
            'コード', '名称', '連動対象指標', '管理会社名', '信託報酬',
            '終値(円)', '前日比(%)', '1週比(%)', '2週比(%)', '1年比(%)',
            '配当利回り', '直近配当日'
        ];

        // Format cell content (escape quotes and wrap in quotes if needed)
        const formatCell = (val) => {
            if (val === null || val === undefined) return '""';
            let str = String(val).replace(/"/g, '""'); // escape double quotes
            return `"${str}"`; // enclose in double quotes
        };

        const rows = currentData.map(row => {
            return [
                formatCell(row.code),
                formatCell(row.name),
                formatCell(row.benchmark),
                formatCell(row.management),
                formatCell(row.fee),
                row.price !== null ? row.price : '""',
                row.change_1d_pct !== null ? row.change_1d_pct : '""',
                row.change_1w_pct !== null ? row.change_1w_pct : '""',
                row.change_2w_pct !== null ? row.change_2w_pct : '""',
                row.change_1y_pct !== null ? row.change_1y_pct : '""',
                formatCell(row.dividend_yield),
                formatCell(row.dividend_date)
            ].join(',');
        });

        const csvContent = BOM + headers.join(',') + '\n' + rows.join('\n');

        // Create Blob and trigger download
        const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');

        // Generate filename with date
        const dateStr = targetDateDisplay.textContent || new Date().toISOString().split('T')[0];
        link.setAttribute('href', url);
        link.setAttribute('download', `etf_data_${dateStr}.csv`);

        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
    });
});
