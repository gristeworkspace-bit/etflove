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

        const progressBar = document.getElementById('progressBar');
        const progressText = document.getElementById('progressText');
        const progressCount = document.getElementById('progressCount');
        const loadingMessage = document.getElementById('loadingMessage');

        progressBar.style.width = '0%';
        progressText.textContent = '準備中...';
        progressCount.textContent = '0 / 0';
        loadingMessage.textContent = 'JPXのサイトからETF一覧を取得しています...';

        try {
            // 1. Fetch JPX List
            const response = await fetch('/api/fetch_etfs');
            const data = await response.json();

            if (data.status === 'success') {
                targetDateDisplay.textContent = data.target_date;
                let etfList = data.data;

                if (!isFetchAll && etfList.length > limitParam && limitParam !== 0) {
                    etfList = etfList.slice(0, limitParam);
                }

                // Initialize currentData with empty price fields
                currentData = etfList.map(etf => ({
                    ...etf,
                    price: null,
                    change_1d_pct: null,
                    change_1w_pct: null,
                    change_2w_pct: null,
                    change_1y_pct: null,
                    dividend_yield: null,
                    dividend_date: null
                }));

                // Render initial table without prices
                renderTable(currentData);

                loadingStatus.classList.add('hidden');
                dataTable.classList.remove('hidden');
                lastUpdated.classList.remove('hidden');
                downloadCSVBtn.classList.remove('hidden');

                // Show floating progress for the individual fetches
                loadingStatus.classList.remove('hidden');
                loadingMessage.textContent = '各ETFの株価データを取得しています...';

                // 2. Fetch prices individually
                await fetchAllPrices(currentData, progressBar, progressText, progressCount, data.target_date);

                loadingStatus.classList.add('hidden');
                downloadCSVBtn.disabled = false;
                fetchDataBtn.disabled = false;
                fetchAllToggle.disabled = false;

            } else {
                showError(data.error || "データ取得に失敗しました");
            }
        } catch (error) {
            console.error("Fetch error:", error);
            showError("JPXデータの取得に失敗しました。サーバーが動いているか確認してください。");
        }
    });

    function showError(msg) {
        loadingStatus.classList.add('hidden');
        noDataMessage.innerHTML = `<i class="fas fa-exclamation-triangle val-negative" style="font-size: 2rem; margin-bottom: 1rem;"></i><p class="val-negative">${msg}</p>`;
        noDataMessage.classList.remove('hidden');
        fetchDataBtn.disabled = false;
        fetchAllToggle.disabled = false;
    }

    // Date computation helpers
    function getBusinessDates(targetDateStr) {
        const targetDate = new Date(targetDateStr);
        const daysList = [];
        let curr = new Date(targetDate);

        while (daysList.length < 300) {
            if (curr.getDay() !== 0 && curr.getDay() !== 6) {
                daysList.push(new Date(curr));
            }
            curr.setDate(curr.getDate() - 1);
        }

        return {
            target: daysList[0],
            prev: daysList[1],
            week: daysList[5],
            two_weeks: daysList[10],
            year: daysList[daysList.length - 1]
        };
    }

    function formatPctChange(oldPrice, newPrice) {
        if (!oldPrice || !newPrice || oldPrice === 0) return null;
        return ((newPrice - oldPrice) / oldPrice) * 100;
    }

    async function fetchAllPrices(etfList, progressBar, progressText, progressCount, targetDateStr) {
        const total = etfList.length;
        const bDates = getBusinessDates(targetDateStr);

        for (let i = 0; i < total; i++) {
            const row = etfList[i];

            // Update Progress UI
            const pct = Math.round(((i + 1) / total) * 100);
            progressBar.style.width = `${pct}%`;
            progressText.textContent = `${row.code} ${row.name || ''}`;
            progressCount.textContent = `${i + 1} / ${total}`;

            try {
                // Add tiny client-side delay to spread requests
                await new Promise(r => setTimeout(r, 200));

                const res = await fetch(`/api/proxy/yfinance/${row.clean_code}.T`);
                const json = await res.json();

                if (json.status === 'success' && json.data) {
                    const hist = json.data;

                    const getPriceForDate = (targetD) => {
                        let d = new Date(targetD);
                        for (let attempts = 0; attempts < 10; attempts++) {
                            const dStr = d.toISOString().split('T')[0];
                            if (hist[dStr]) return hist[dStr].Close;
                            d.setDate(d.getDate() - 1); // Walk backwards
                        }
                        return null;
                    };

                    const currentPrice = getPriceForDate(bDates.target);
                    const prevPrice = getPriceForDate(bDates.prev);
                    const weekPrice = getPriceForDate(bDates.week);
                    const twoWeekPrice = getPriceForDate(bDates.two_weeks);
                    const yearPrice = getPriceForDate(bDates.year);

                    row.price = currentPrice ? Math.round(currentPrice * 100) / 100 : null;
                    row.change_1d_pct = formatPctChange(prevPrice, currentPrice);
                    row.change_1w_pct = formatPctChange(weekPrice, currentPrice);
                    row.change_2w_pct = formatPctChange(twoWeekPrice, currentPrice);
                    row.change_1y_pct = formatPctChange(yearPrice, currentPrice);

                    row.dividend_yield = "-";
                    row.dividend_date = "-";

                    let annualDiv = 0;
                    const oneYearAgoTime = new Date().getTime() - (365 * 24 * 60 * 60 * 1000);
                    let hasDivs = false;
                    const divDates = [];

                    Object.keys(hist).forEach(dStr => {
                        const dTime = new Date(dStr).getTime();
                        if (hist[dStr].Dividends > 0) {
                            hasDivs = true;
                            divDates.push(new Date(dStr));
                            if (dTime > oneYearAgoTime) {
                                annualDiv += hist[dStr].Dividends;
                            }
                        }
                    });

                    if (hasDivs && currentPrice && currentPrice > 0 && annualDiv > 0) {
                        const calcYield = (annualDiv / currentPrice) * 100;
                        row.dividend_yield = `${calcYield.toFixed(2)}%`;
                    }

                    if (hasDivs && divDates.length > 0) {
                        divDates.sort((a, b) => a - b);
                        const recentDivs = divDates.slice(-24);
                        const today = new Date();

                        const payoutMonths = [...new Set(recentDivs.map(d => d.getMonth() + 1))].sort((a, b) => a - b);
                        const avgDayByMonth = {};
                        payoutMonths.forEach(m => {
                            const days = recentDivs.filter(d => (d.getMonth() + 1) === m).map(d => d.getDate());
                            avgDayByMonth[m] = days.length > 0 ? Math.round(days.reduce((sum, d) => sum + d, 0) / days.length) : 10;
                        });

                        let nextMonth = null;
                        let nextYear = today.getFullYear();
                        let nextDay = null;

                        const currentMonth = today.getMonth() + 1;
                        const currentDay = today.getDate();

                        for (let j = 0; j < payoutMonths.length; j++) {
                            const m = payoutMonths[j];
                            if (m === currentMonth && currentDay < avgDayByMonth[m]) {
                                nextMonth = m;
                                nextDay = avgDayByMonth[m];
                                break;
                            } else if (m > currentMonth) {
                                nextMonth = m;
                                nextDay = avgDayByMonth[m];
                                break;
                            }
                        }

                        if (!nextMonth && payoutMonths.length > 0) {
                            nextMonth = payoutMonths[0];
                            nextYear += 1;
                            nextDay = avgDayByMonth[nextMonth];
                        }

                        if (nextMonth && nextDay) {
                            row.dividend_date = `次回予想: ${nextYear}年${nextMonth}月${nextDay}日頃`;
                        }
                    }

                    renderTable(currentData);
                }
            } catch (err) {
                console.error(`Error fetching price for ${row.code}:`, err);
            }
        }
    }

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
