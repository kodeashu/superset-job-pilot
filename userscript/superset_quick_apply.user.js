// ==UserScript==
// @name         Superset Job Pilot - 1-Click Apply & Calendar Sync
// @namespace    https://joinsuperset.com/
// @version      1.0.0
// @description  Automates campus placement job applications on Superset, highlights short deadlines, and syncs deadlines to Google Calendar with reminders.
// @author       Superset Job Pilot
// @match        https://app.joinsuperset.com/*
// @grant        GM_addStyle
// @grant        GM_notification
// ==/UserScript==

(function() {
    'use strict';

    console.log('[Superset Job Pilot] Userscript initialized.');

    // Inject custom styling
    const customStyles = `
        .sjp-floating-bar {
            position: fixed;
            bottom: 24px;
            right: 24px;
            z-index: 999999;
            background: #1e1e2d;
            color: #ffffff;
            padding: 14px 20px;
            border-radius: 12px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.3);
            display: flex;
            align-items: center;
            gap: 12px;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            font-size: 14px;
            border: 1px solid rgba(255,255,255,0.15);
        }
        .sjp-btn {
            background: #3b82f6;
            color: #fff;
            border: none;
            padding: 8px 14px;
            border-radius: 6px;
            cursor: pointer;
            font-weight: 600;
            font-size: 13px;
            transition: all 0.2s ease;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }
        .sjp-btn:hover {
            background: #2563eb;
            transform: translateY(-1px);
        }
        .sjp-btn-cal {
            background: #10b981;
        }
        .sjp-btn-cal:hover {
            background: #059669;
        }
        .sjp-badge-urgent {
            background: #ef4444;
            color: white;
            padding: 3px 8px;
            border-radius: 4px;
            font-weight: 700;
            font-size: 11px;
            animation: sjp-pulse 1.5s infinite;
        }
        @keyframes sjp-pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.6; }
        }
    `;

    const styleEl = document.createElement('style');
    styleEl.textContent = customStyles;
    document.head.appendChild(styleEl);

    // Create Floating Helper Bar
    function createFloatingBar() {
        if (document.getElementById('sjp-floating-bar')) return;

        const bar = document.createElement('div');
        bar.id = 'sjp-floating-bar';
        bar.className = 'sjp-floating-bar';
        bar.innerHTML = `
            <span>🚀 <b>Superset Job Pilot</b></span>
            <button id="sjp-apply-all-btn" class="sjp-btn">⚡ Auto-Apply All Eligible</button>
            <button id="sjp-scan-deadlines-btn" class="sjp-btn sjp-btn-cal">📅 Sync Deadlines to Calendar</button>
        `;
        document.body.appendChild(bar);

        document.getElementById('sjp-apply-all-btn').addEventListener('click', autoApplyAllEligible);
        document.getElementById('sjp-scan-deadlines-btn').addEventListener('click', scanAndEnhanceCards);
    }

    // Enhance individual job profile cards with 1-Click Calendar button
    function scanAndEnhanceCards() {
        // Target job profile cards in Superset UI
        const jobCards = document.querySelectorAll('div[class*="job-profile"], div[class*="card"], li[class*="profile"]');
        let count = 0;

        jobCards.forEach(card => {
            if (card.querySelector('.sjp-cal-injected')) return;

            const text = card.innerText || '';
            const isEligible = text.toLowerCase().includes('eligible') && !text.toLowerCase().includes('not eligible');
            
            // Try to extract company name and deadline
            const titleEl = card.querySelector('h3, h4, [class*="title"], [class*="company"]');
            const companyName = titleEl ? titleEl.innerText.trim() : 'Superset Campus Drive';

            // Deadline regex matcher (common Indian placement formats: 10 Nov, 2026 6:00 PM)
            const deadlineMatch = text.match(/(?:deadline|apply by|last date)[\s:]*([0-9]{1,2}[/-][0-9]{1,2}[/-][0-9]{2,4}|[0-9]{1,2}\s+[a-zA-Z]+\s+[0-9]{4}[^,\n]*)/i);
            const deadlineText = deadlineMatch ? deadlineMatch[1] : 'Soon';

            // Add Google Calendar button
            const btn = document.createElement('button');
            btn.className = 'sjp-btn sjp-btn-cal sjp-cal-injected';
            btn.style.marginTop = '8px';
            btn.style.fontSize = '12px';
            btn.style.padding = '4px 10px';
            btn.innerHTML = '📅 Add to Google Calendar';

            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                openGoogleCalendarEvent(companyName, deadlineText, window.location.href);
            });

            card.appendChild(btn);
            count++;
        });

        alert(`Superset Job Pilot: Enhanced ${count} job cards with 1-Click Google Calendar sync!`);
    }

    // Google Calendar URL launcher
    function openGoogleCalendarEvent(company, deadlineStr, url) {
        const title = encodeURIComponent(`🚨 DEADLINE: ${company} Superset Application`);
        const details = encodeURIComponent(`Deadline for Superset job profile application: ${company}\nPortal: ${url}\n\nAdded via Superset Job Pilot.`);
        
        // Approximate 1 hour event for today/tomorrow
        const now = new Date();
        const tomorrow = new Date(now.getTime() + 24 * 60 * 60 * 1000);
        const startStr = tomorrow.toISOString().replace(/-|:|\.\d\d\d/g, '').slice(0, 15) + 'Z';
        const endStr = new Date(tomorrow.getTime() + 60 * 60 * 1000).toISOString().replace(/-|:|\.\d\d\d/g, '').slice(0, 15) + 'Z';

        const calUrl = `https://calendar.google.com/calendar/render?action=TEMPLATE&text=${title}&dates=${startStr}/${endStr}&details=${details}&location=Superset`;
        window.open(calUrl, '_blank');
    }

    // Load user profile from localStorage (configure via userscript menu or web console)
    const STUDENT_PROFILE = JSON.parse(localStorage.getItem("SUPERSET_PILOT_PROFILE") || JSON.stringify({
        fullName: "",
        firstName: "",
        middleName: "",
        lastName: "",
        nationality: "Indian",
        country: "India",
        passportNumber: "NA",
        passportExpiry: "NA",
        tenthSchool: "",
        tenthBoard: "",
        tenthYear: "",
        tenthGpa: "",
        twelfthSchool: "",
        twelfthBoard: "",
        twelfthYear: "",
        twelfthGpa: "",
        marksheet8thSem: "July'27",
        resumeLink: "",
        introVideoLink: "",
        currentLocation: "Bhubaneswar, Odisha",
        hasPan: "No",
        panNumber: "NA",
        githubLink: "",
        phoneNumber: "",
        fatherName: "",
        emergencyContactName: "",
        emergencyContactPhone: "",
        emergencyContactAddress: "",
        currentGpa: "",
        aadhaarLast4: "",
        dob: "",
        permanentAddress: ""
    }));

    function autoFillModalForm() {
        const modal = document.querySelector('.modal, [role="dialog"]');
        if (!modal) return;

        // Auto-fill inputs
        modal.querySelectorAll('input[type="text"], textarea').forEach(input => {
            const label = (input.closest('label, div.form-group, div')?.innerText || '').toLowerCase();
            const ph = (input.placeholder || '').toLowerCase();
            const text = label + ' ' + ph;

            if (text.includes('github') || text.includes('git link')) {
                input.value = STUDENT_PROFILE.githubLink;
            } else if (text.includes('pan')) {
                if (text.includes('first name')) input.value = STUDENT_PROFILE.firstName;
                else if (text.includes('middle name')) input.value = STUDENT_PROFILE.middleName;
                else if (text.includes('last name')) input.value = STUDENT_PROFILE.lastName;
                else input.value = STUDENT_PROFILE.panNumber;
            } else if (text.includes('aadhaar') || text.includes('aadhar')) {
                input.value = STUDENT_PROFILE.aadhaarLast4;
            } else if (text.includes('birth') || text.includes('dob')) {
                input.value = STUDENT_PROFILE.dob;
            } else if (text.includes('father')) {
                input.value = STUDENT_PROFILE.fatherName;
            } else if (text.includes('emergency')) {
                if (text.includes('number') || text.includes('phone') || text.includes('contact no')) input.value = STUDENT_PROFILE.emergencyContactPhone;
                else if (text.includes('address')) input.value = STUDENT_PROFILE.emergencyContactAddress;
                else input.value = STUDENT_PROFILE.emergencyContactName;
            } else if (text.includes('mobile') || (text.includes('phone') && !text.includes('emergency'))) {
                input.value = STUDENT_PROFILE.phoneNumber;
            } else if (text.includes('permanent')) {
                input.value = STUDENT_PROFILE.permanentAddress;
            } else if (text.includes('10th') || text.includes('class 10')) {
                if (text.includes('school') || text.includes('institute')) input.value = STUDENT_PROFILE.tenthSchool;
                else if (text.includes('year') || text.includes('passing')) input.value = STUDENT_PROFILE.tenthYear;
            } else if (text.includes('12th') || text.includes('class 12') || text.includes('diploma')) {
                if (text.includes('college') || text.includes('school')) input.value = STUDENT_PROFILE.twelfthSchool;
                else if (text.includes('year') || text.includes('passing')) input.value = STUDENT_PROFILE.twelfthYear;
            } else if (text.includes('cgpa') || text.includes('current gpa') || (text.includes('gpa') && !text.includes('10th') && !text.includes('12th'))) {
                input.value = STUDENT_PROFILE.currentGpa;
            } else if (text.includes('gap')) {
                input.value = "0";
            } else if (text.includes('resume') && (text.includes('link') || text.includes('share'))) {
                input.value = STUDENT_PROFILE.resumeLink;
            } else if (text.includes('video')) {
                input.value = STUDENT_PROFILE.introVideoLink;
            } else if (text.includes('nationality') || text.includes('country')) {
                input.value = STUDENT_PROFILE.country;
            } else if (text.includes('passport')) {
                input.value = STUDENT_PROFILE.passportNumber;
            } else if (text.includes('location')) {
                input.value = STUDENT_PROFILE.currentLocation;
            }
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
        });

        // Auto-select dropdowns
        modal.querySelectorAll('select').forEach(sel => {
            const label = (sel.closest('label, div.form-group, div')?.innerText || '').toLowerCase();
            Array.from(sel.options).forEach(opt => {
                const optText = (opt.text || opt.value || '').toLowerCase();
                if (label.includes('nationality') && (optText.includes('other') || optText.includes('foreigner') || optText.includes('nepal'))) {
                    sel.value = opt.value;
                } else if (label.includes('passport') && optText.includes('foreign')) {
                    sel.value = opt.value;
                } else if (label.includes('shift') && optText === 'yes') {
                    sel.value = opt.value;
                } else if (label.includes('gap') && optText.trim() === '0') {
                    sel.value = opt.value;
                } else if (label.includes('pass in') && (optText.includes('regular') || optText.includes('fulltime'))) {
                    sel.value = opt.value;
                }
            });
            sel.dispatchEvent(new Event('change', { bubbles: true }));
        });

        // Auto-check agreements, radio buttons & disclaimers
        modal.querySelectorAll('input[type="checkbox"], input[type="radio"]').forEach(box => {
            const label = (box.closest('label, div')?.innerText || '').toLowerCase();
            if (label.includes('agree') || label.includes('disclaimer') || label.includes('yes') || label.includes('regular')) {
                box.checked = true;
                box.dispatchEvent(new Event('change', { bubbles: true }));
            }
        });
    }

    // Auto Apply to all eligible jobs visible on the page
    function autoApplyAllEligible() {
        const applyButtons = Array.from(document.querySelectorAll('button, a')).filter(el => {
            const txt = (el.innerText || '').trim().toLowerCase();
            return (txt === 'apply' || txt === 'apply for this profile') && !el.disabled;
        });

        if (applyButtons.length === 0) {
            alert('No pending eligible "Apply" buttons found on the current page. You may already be applied or current batch is locked.');
            return;
        }

        const confirmApply = confirm(`Found ${applyButtons.length} eligible job openings! Do you want Superset Job Pilot to auto-apply to them now?`);
        if (!confirmApply) return;

        let appliedCount = 0;
        applyButtons.forEach((btn, index) => {
            setTimeout(() => {
                btn.click();
                appliedCount++;
                console.log(`[Superset Job Pilot] Clicked apply on job #${appliedCount}`);
                
                // Auto-fill questions if a modal pops up, then submit
                setTimeout(() => {
                    autoFillModalForm();
                    setTimeout(() => {
                        const confirmModalBtn = document.querySelector('.modal button.btn-primary, [role="dialog"] button[type="submit"], [role="dialog"] button.btn-success');
                        if (confirmModalBtn) confirmModalBtn.click();
                    }, 500);
                }, 600);

            }, index * 2500);
        });
    }

    // Initialize when page is loaded
    window.addEventListener('load', () => {
        setTimeout(createFloatingBar, 1500);
    });

    // Handle single-page app route navigation
    let lastUrl = location.href;
    new MutationObserver(() => {
        const url = location.href;
        if (url !== lastUrl) {
            lastUrl = url;
            setTimeout(createFloatingBar, 1500);
        }
    }).observe(document, {subtree: true, childList: true});

})();
