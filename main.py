import os
import sys
import time
import json
import glob
import logging
import re
import random
import threading
import hashlib
import base64
import shutil
import tempfile
import requests
from difflib import SequenceMatcher
from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright

try:
    from colorama import Fore, Style, init as colorama_init
    colorama_init(autoreset=True)
    HAS_COLOR = True
except ImportError:
    HAS_COLOR = False
    class _DummyColor:
        def __getattr__(self, name):
            return ''
    Fore = _DummyColor()
    Style = _DummyColor()

def _load_speed_multiplier(default=5.0):
    try:
        config_path = os.path.join("config", "speed_config.json")
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        value = float(data.get("GLOBAL_SPEED_MULTIPLIER", default))
        if 0.5 <= value <= 10.0:
            return value
    except Exception:
        pass
    return default

def _load_audio_speed_multiplier(default=1.0):
    try:
        config_path = os.path.join("config", "speed_config.json")
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        value = float(data.get("AUDIO_SPEED_MULTIPLIER", default))
        if 0.5 <= value <= 3.0:
            return value
    except Exception:
        pass
    return default

GLOBAL_SPEED_MULTIPLIER = _load_speed_multiplier(5.0)

_BASE_ACTION_SETTLE = 0.8
_BASE_EXERCISE_STEP_PAUSE = 0.4
_BASE_FAST_ACTION_SETTLE = 0.15
_BASE_DRAG_SYNC = 0.12
_BASE_DRAG_CLICK = 0.25
_BASE_DRAG_PLACE = 0.10

ACTION_SETTLE_SECONDS = _BASE_ACTION_SETTLE / GLOBAL_SPEED_MULTIPLIER
EXERCISE_STEP_PAUSE_SECONDS = _BASE_EXERCISE_STEP_PAUSE / GLOBAL_SPEED_MULTIPLIER
FAST_ACTION_SETTLE_SECONDS = _BASE_FAST_ACTION_SETTLE / GLOBAL_SPEED_MULTIPLIER
DRAG_SYNC_INTERVAL = _BASE_DRAG_SYNC / GLOBAL_SPEED_MULTIPLIER
DRAG_CLICK_SETTLE = _BASE_DRAG_CLICK / GLOBAL_SPEED_MULTIPLIER
DRAG_PLACE_SETTLE = _BASE_DRAG_PLACE / GLOBAL_SPEED_MULTIPLIER

AUDIO_SPEED_MULTIPLIER = _load_audio_speed_multiplier(1.0)

_AUDIO_MODAL_DETECT_JS = r"""
() => {
    const norm = v => (v || '').replace(/\u00a0/g, ' ').toLowerCase();
    const isVisible = el => {
        if (!el) return false;
        const s = window.getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden') return false;
        if (Number(s.opacity) === 0) return false;
        const r = el.getBoundingClientRect();
        return r.width > 4 && r.height > 4;
    };
    const isCompletion = el => !!el.querySelector(
        '.circliful-and-results, #nextButton, .exercise-result-container, ' +
        '.circliful-container-results'
    );

    const roots = Array.from(document.querySelectorAll(
        '.modal, .modal-dialog, .modal-content, [role="dialog"], ' +
        '[class*="audio-setup"], [id*="audio-setup"]'
    )).filter(isVisible);

    for (const el of roots) {
        if (isCompletion(el)) continue;

        const text = norm(el.innerText);
        const titleEl = el.querySelector('#ModalTitle, .modal-title, h1, h2, h3, h4, h5');
        const title = norm(titleEl ? titleEl.textContent : '');
        const probe = text + ' ' + title;

        const hasRange = !!el.querySelector(
            'input[type="range"], input[type="slider"], .slider, .ui-slider'
        );
        const hasSelect = !!el.querySelector('select');
        const hasMic = !!el.querySelector(
            '.microphone-button, [class*="microphone"], [class*="mic-"], ' +
            '.icon-mic, button[class*="record"]'
        );
        const marker = [
            'audio setup', 'audio-setup', 'playback volume',
            'microphone sensitivity', 'microphone', 'recording device',
            'การตั้งค่าเสียง', 'ตั้งค่าเสียง', 'ระดับเสียง',
            'ความไวไมโครโฟน', 'ไมโครโฟน', 'อุปกรณ์บันทึกเสียง'
        ].some(m => probe.includes(m));
        const structural = (hasRange && (hasSelect || hasMic)) || (
            (el.classList.contains('pronunciation-wizard') ||
             el.classList.contains('modal-courseware')) &&
            (hasRange || hasSelect || hasMic)
        );

        if (marker || structural) return true;
    }
    return false;
}
"""

_AUDIO_MODAL_CLOSE_JS = r"""
() => {
    const norm = v => (v || '').replace(/\u00a0/g, ' ').toLowerCase();
    const isVisible = el => {
        if (!el) return false;
        const s = window.getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden') return false;
        if (Number(s.opacity) === 0) return false;
        const r = el.getBoundingClientRect();
        return r.width > 4 && r.height > 4;
    };
    const isCompletion = el => !!el.querySelector(
        '.circliful-and-results, #nextButton, .exercise-result-container, ' +
        '.circliful-container-results'
    );

    const roots = Array.from(document.querySelectorAll(
        '.modal, .modal-dialog, .modal-content, [role="dialog"], ' +
        '[class*="audio-setup"], [id*="audio-setup"]'
    )).filter(isVisible);

    let root = null;
    for (const el of roots) {
        if (isCompletion(el)) continue;
        const text = norm(el.innerText);
        const titleEl = el.querySelector('#ModalTitle, .modal-title, h1, h2, h3, h4, h5');
        const probe = text + ' ' + norm(titleEl ? titleEl.textContent : '');
        const hasRange = !!el.querySelector(
            'input[type="range"], input[type="slider"], .slider, .ui-slider'
        );
        const hasSelect = !!el.querySelector('select');
        const hasMic = !!el.querySelector(
            '.microphone-button, [class*="microphone"], [class*="mic-"], ' +
            '.icon-mic, button[class*="record"]'
        );
        const marker = [
            'audio setup', 'audio-setup', 'playback volume',
            'microphone sensitivity', 'microphone', 'recording device',
            'การตั้งค่าเสียง', 'ตั้งค่าเสียง', 'ระดับเสียง',
            'ความไวไมโครโฟน', 'ไมโครโฟน', 'อุปกรณ์บันทึกเสียง'
        ].some(m => probe.includes(m));
        const structural = (hasRange && (hasSelect || hasMic)) || (
            (el.classList.contains('pronunciation-wizard') ||
             el.classList.contains('modal-courseware')) &&
            (hasRange || hasSelect || hasMic)
        );
        if (marker || structural) { root = el; break; }
    }
    if (!root) return false;

    // 1) Standard bootstrap close controls.
    const selectors = [
        'button.close', '[data-dismiss="modal"]', '.modal-header .close',
        '.modal-header button', 'button[aria-label="Close"]',
        'button[aria-label="close"]', 'button[aria-label="ปิด"]',
        'button[title="Close"]', 'button[title="ปิด"]', '.close'
    ];
    for (const sel of selectors) {
        for (const btn of root.querySelectorAll(sel)) {
            if (btn && isVisible(btn)) {
                btn.click();
                return true;
            }
        }
    }

    // 2) Any button whose icon/label clearly means close (any language).
    for (const btn of root.querySelectorAll('button, a[role="button"]')) {
        if (!isVisible(btn)) continue;
        const label = norm(
            (btn.getAttribute('aria-label') || '') + ' ' +
            (btn.getAttribute('title') || '') + ' ' +
            (btn.textContent || '')
        ).trim();
        if (label === 'x' || label === '×' || label === '✕' ||
            label.includes('close') || label.includes('ปิด')) {
            btn.click();
            return true;
        }
    }

    // 3) Let jQuery/Bootstrap hide it.
    if (window.jQuery) {
        try { window.jQuery(root).modal('hide'); return true; } catch (e) {}
    }

    // 4) Last resort: hide the dialog and remove the blocking backdrop.
    root.classList.remove('in', 'show');
    root.style.display = 'none';
    root.setAttribute('aria-hidden', 'true');
    document.querySelectorAll('.modal-backdrop').forEach(b => b.remove());
    document.body.classList.remove('modal-open');
    return true;
}
"""

def audio_setup_modal_visible(page):
    try:
        return bool(page.evaluate(_AUDIO_MODAL_DETECT_JS))
    except Exception:
        return False

_GREEN_MARKER_JS = r"""
() => {
    const els = Array.from(document.querySelectorAll(
        '.icon-acepted-checkmark.status-green, [class*="success"], [class*="correct"]'
    ));
    const isVisible = el => {
        const s = window.getComputedStyle(el);
        if (!s) return false;
        if (s.display === 'none' || s.visibility === 'hidden') return false;
        if (Number(s.opacity) === 0) return false;
        const r = el.getBoundingClientRect();
        return r.width > 4 && r.height > 4;
    };
    const isControl = el => !!el.closest(
        'button, a, label, input, select, textarea, [role="button"]'
    );
    return els.some(el => isVisible(el) && !isControl(el));
}
"""

def dismiss_audio_setup_modal(page, logger=None, step=None):
    log = logger.info if logger is not None else (lambda *a, **k: None)
    if not audio_setup_modal_visible(page):
        return False

    log("[AUDIO] Audio setup modal detected — closing (language-agnostic).")
    closed_via = ""
    try:
        page.evaluate(_AUDIO_MODAL_CLOSE_JS)
        closed_via = "close control"
    except Exception as e:
        log(f"[AUDIO] JS close failed: {str(e)[:120]}")

    time.sleep(0.35)
    if not audio_setup_modal_visible(page):
        if step is not None:
            step("Closed Audio setup modal")
        log("[AUDIO] Audio setup modal closed.")
        return True

    for key in ("Escape", "Esc"):
        try:
            page.keyboard.press(key)
            time.sleep(0.35)
            if not audio_setup_modal_visible(page):
                closed_via = "Escape"
                break
        except Exception:
            pass

    if not audio_setup_modal_visible(page):
        if step is not None:
            step(f"Closed Audio setup modal via {closed_via}")
        log("[AUDIO] Audio setup modal closed.")
        return True

    try:
        page.evaluate("""
            () => {
                document.querySelectorAll('.modal-backdrop').forEach(b => b.remove());
                document.body.classList.remove('modal-open');
                document.body.style.removeProperty('overflow');
                document.body.style.removeProperty('padding-right');
            }
        """)
    except Exception:
        pass
    time.sleep(0.2)
    still_visible = audio_setup_modal_visible(page)
    log("[AUDIO] Audio setup modal closed (forced)." if not still_visible
        else "[AUDIO] Audio setup modal still visible after all close attempts.")
    if not still_visible and step is not None:
        step("Closed Audio setup modal (forced)")
    return not still_visible

TRANSLATIONS = {
    "th": {
        "select_lang": "เลือกภาษา / Select Language",
        "lang_th": "ภาษาไทย",
        "lang_en": "English",

        "login_menu": "เมนูเข้าสู่ระบบ",
        "opt_login": "เริ่มต้นเบราว์เซอร์และเข้าสู่ระบบ",
        "opt_manage_acc": "จัดการบัญชี",
        "opt_exit": "ออกจากโปรแกรม",

        "select_account": "เลือกบัญชี",
        "add_account": "เพิ่มบัญชีใหม่",
        "delete_pwd": "ลบรหัสผ่านที่บันทึกไว้",
        "delete_acc": "ลบบัญชี",
        "no_accounts": "ยังไม่มีบัญชีที่บันทึกไว้",
        "saved_pwd": "บันทึกรหัสผ่านแล้ว",
        "deleted_pwd": "ลบรหัสผ่านของ {} แล้ว",
        "deleted_acc": "ลบบัญชี {} แล้ว",

        "dashboard_menu": "เมนูหลัก (Dashboard)",
        "opt_select_chapter": "เลือกบทเรียนและดูแบบฝึกหัด",
        "opt_results": "ไปที่หน้า Results",
        "opt_search_answer": "ค้นหาคำตอบจากโจทย์ที่คัดลอก",
        "opt_logout": "ออกจากระบบ",

        "available_chapters": "บทเรียนที่มีให้เลือก",
        "select_chapter": "เลือกบทเรียน: ",
        "no_chapters": "ไม่พบบทเรียนที่ปลดล็อค",

        "exercise_list": "รายการแบบฝึกหัด",
        "select_exercises": "เลือกแบบฝึกหัด (เช่น 1, 1-5, 1,3,5, all): ",
        "no_exercises": "ไม่พบแบบฝึกหัด",
        "table_no": "No.",
        "table_title": "หัวข้อ",
        "table_date": "วันที่",
        "table_time": "เวลารวม",
        "table_score": "คะแนน",
        "table_review": "ทบทวน",

        "exercise_menu": "เมนูแบบฝึกหัด",
        "opt_view": "เปิดดูเฉยๆ (Just view)",
        "opt_dump": "Dump ทุกหน้า",
        "opt_auto_solve": "ทำแบบฝึกหัดอัตโนมัติ (Auto Solve)",
        "opt_new_learning_path": "ทำแบบฝึกหัดใหม่ต่อเนื่อง (Auto Continue)",
        "select_pages": "เลือกข้อที่ต้องการทำ (เช่น 1, 1-5, all): ",

        "auto_solve": "เริ่มทำแบบฝึกหัดอัตโนมัติ",
        "will_not_move": "จะไม่ไปข้อถัดไปจนกว่าจะได้คะแนน >= 51",
        "found_pages": "พบทั้งหมด {} หน้า",
        "page": "หน้า",
        "type": "ประเภท",
        "success": "สำเร็จ",
        "failed": "ล้มเหลว",
        "skipped": "ข้าม",
        "retrying": "กำลังลองใหม่ ครั้งที่ {}/{}...",
        "max_retries": "ลองใหม่ครบกำหนดแล้ว",

        "summary": "สรุปผลการทำแบบฝึกหัด",
        "pass": "ผ่าน",
        "fail": "ไม่ผ่าน",
        "skip": "ข้าม",
        "score": "คะแนน",
        "time_spent": "เวลาที่ใช้",
        "total_time": "เวลารวม",
        "passed_count": "ผ่านทั้งหมด",
        "failed_count": "ไม่ผ่าน",
        "skipped_count": "ข้าม",
        "selected_only": "ทำเฉพาะข้อที่เลือกไว้",
        "finished_selected": "ทำครบตามที่เลือกไว้แล้ว",
        "next_action": "ขั้นตอนถัดไป",
        "back_to_list": "กลับไปหน้ารายการแบบฝึกหัด",
        "back_to_main": "กลับไปเมนูหลัก",
        "exit_app": "ออกจากโปรแกรม",

        "repeat": "ทำซ้ำ (Repeat)",
        "continue": "เรียนรู้ต่อ (Continue)",
        "nothing": "ไม่ทำอะไร (กลับเมนูหลัก)",
        "what_next": "ต้องการทำอะไรต่อ?",

        "back": "ย้อนกลับ",
        "invalid_choice": "ตัวเลือกไม่ถูกต้อง",
        "press_enter": "กด Enter เพื่อดำเนินการต่อ...",
        "cancel": "ยกเลิก",
    },
    "en": {
        "select_lang": "Select Language",
        "lang_th": "Thai",
        "lang_en": "English",

        "login_menu": "Login Menu",
        "opt_login": "Start Browser & Login",
        "opt_manage_acc": "Manage Accounts",
        "opt_exit": "Exit Program",

        "select_account": "Select Account",
        "add_account": "Add New Account",
        "delete_pwd": "Delete Saved Password",
        "delete_acc": "Delete Account",
        "no_accounts": "No accounts saved yet",
        "saved_pwd": "Password saved",
        "deleted_pwd": "Deleted password for {}",
        "deleted_acc": "Deleted account {}",

        "dashboard_menu": "Dashboard Menu",
        "opt_select_chapter": "Select Chapter & View Exercises",
        "opt_results": "Go to Results Page",
        "opt_search_answer": "Search answer from copied question",
        "opt_logout": "Logout",

        "available_chapters": "Available Chapters",
        "select_chapter": "Select Chapter: ",
        "no_chapters": "No unlocked chapters found",

        "exercise_list": "Exercise List",
        "select_exercises": "Select exercises (e.g. 1, 1-5, 1,3,5, all): ",
        "no_exercises": "No exercises found",
        "table_no": "No.",
        "table_title": "Title",
        "table_date": "Date",
        "table_time": "Time",
        "table_score": "Score",
        "table_review": "Review",

        "exercise_menu": "Exercise Menu",
        "opt_view": "Open (Just view)",
        "opt_dump": "Dump All Pages",
        "opt_auto_solve": "Auto Solve (Keep trying until 51)",
        "opt_new_learning_path": "New learning path (start at first selected and auto-Continue)",
        "select_pages": "Select pages (e.g. 1, 1-5, all): ",

        "auto_solve": "Auto Solve",
        "will_not_move": "Will NOT move to next page until current page gets >= 51.",
        "found_pages": "Found {} pages.",
        "page": "Page",
        "type": "Type",
        "success": "SUCCESS",
        "failed": "FAILED",
        "skipped": "SKIPPED",
        "retrying": "Retrying Page ({}/{})...",
        "max_retries": "Max retries reached.",

        "summary": "Exercise Summary",
        "pass": "PASS",
        "fail": "FAIL",
        "skip": "SKIP",
        "score": "Score",
        "time_spent": "Time Spent",
        "total_time": "Total Time",
        "passed_count": "Passed",
        "failed_count": "Failed",
        "skipped_count": "Skipped",
        "selected_only": "Solving only selected pages",
        "finished_selected": "Finished all selected pages",
        "next_action": "Next Action",
        "back_to_list": "Back to Exercise List",
        "back_to_main": "Back to Main Menu",
        "exit_app": "Exit Program",

        "repeat": "Repeat",
        "continue": "Continue",
        "nothing": "Do Nothing (Back to Main Menu)",
        "what_next": "What do you want to do next?",

        "back": "Back",
        "invalid_choice": "Invalid choice",
        "press_enter": "Press Enter to continue...",
        "cancel": "Cancel",
    }
}

UNSUPPORTED_TYPES = {
    "type-intro": "introductory page",
}

class BaseSolver:
    PASS_LINE = 51

    def __init__(self, page, logger, answer_db=None):
        self.page = page
        self.logger = logger
       
        self.answer_db = answer_db if answer_db is not None else {}
        self.start_time = None
        self.end_time = None

    def solve(self):
        raise NotImplementedError

    def start_timer(self):
        self.start_time = time.time()

    def stop_timer(self):
        if not self.start_time:
            return "N/A"
        self.end_time = time.time()
        elapsed = self.end_time - self.start_time
        minutes = int(elapsed // 60)
        seconds = elapsed % 60
        if minutes > 0:
            return f"{minutes}m {seconds:.2f}s"
        return f"{seconds:.2f}s"

    def step(self, msg):
        print(f"    >> {msg}")
        self.logger.info(f"STEP: {msg}")

    def _normalise_question_text(self, text):
        if text is None:
            return ""
        cleaned = str(text).replace("\xa0", " ").replace("\n", " ").replace("\t", " ")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned.casefold()

    def _find_cached_answer(self, question_text=None):
        if not self.answer_db:
            return None

        source = question_text if question_text is not None else self._extract_question_text()
        if source is None:
            return None

        exact = self.answer_db.get(source)
        if exact is not None:
            return exact

        source_norm = self._normalise_question_text(source)
        if not source_norm:
            return None

        best_key = None
        best_value = None
        best_score = 0.0
        for key, value in self.answer_db.items():
            if not isinstance(key, str):
                continue
            key_norm = self._normalise_question_text(key)
            if not key_norm:
                continue
            if key_norm == source_norm:
                return value
            if source_norm in key_norm or key_norm in source_norm:
                return value
            score = SequenceMatcher(None, source_norm, key_norm).ratio()
            if score > best_score:
                best_score = score
                best_key = key
                best_value = value

        if best_score >= 0.82:
            return best_value
        return None

    def _extract_question_text(self):
       
        try:
           
            parts = []
            for sel in [".instructions-text", "h1.exercise-header",
                        ".exercise-items", ".gap-container"]:
                el = self.page.locator(sel).first
                if el.count() > 0:
                    txt = el.inner_text().strip()
                    if txt:
                        parts.append(txt)
            if parts:
                return " | ".join(parts)
        except Exception:
            pass
        return "Unknown Question"

    def _scroll_into_view(self, locator, timeout=1000):
        try:
            if locator.count() > 0:
                locator.first.scroll_into_view_if_needed(timeout=timeout)
                time.sleep(0.05)
                return True
        except Exception as e:
            self.logger.debug(f"[SCROLL] Failed to scroll into view: {str(e)[:100]}")
        return False

    def _scroll_to_center(self, locator, timeout=1000):
        try:
            if locator.count() == 0:
                return False
            locator.first.evaluate(
                "el => el.scrollIntoView({block: 'center', inline: 'nearest'})"
            )
            time.sleep(0.1)
            return True
        except Exception as e:
            self.logger.debug(f"[SCROLL] center failed: {str(e)[:100]}")
            return self._scroll_into_view(locator, timeout=timeout)

    def _click_choice_option(self, option, description="choice"):
        try:
            if option.count() == 0:
                return False

            clickable = None
            for sel in (
                "xpath=ancestor::label[1]",
                "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), "
                "' choice-option ')][1]",
            ):
                try:
                    ancestor = option.locator(sel)
                    if ancestor.count() > 0:
                        clickable = ancestor.first
                        break
                except Exception:
                    continue

            if clickable is None:
                clickable = option

            self._scroll_to_center(clickable)

            try:
                clickable.click(timeout=3000)
                return True
            except Exception as e:
                self.logger.debug(
                    f"[CHOICE] normal click failed ({description}): {str(e)[:100]}")

            try:
                option.evaluate("el => el.click()")
                return True
            except Exception as e:
                self.logger.debug(
                    f"[CHOICE] JS click failed ({description}): {str(e)[:100]}")
            return False
        except Exception as e:
            self.logger.debug(f"[CHOICE] click error ({description}): {e}")
            return False

    def _safe_click(self, locator, timeout=1500, description=""):
        try:
            if locator.count() == 0:
                return False
            if not locator.is_visible(timeout=800):
                return False
        except Exception:
            return False

        self._scroll_into_view(locator)

        try:
            locator.click(timeout=timeout)
            return True
        except Exception as e:
            self.logger.debug(f"[SAFE_CLICK] normal fail ({description}): {str(e)[:100]}")

        try:
            self.page.evaluate("""
                () => {
                    document.querySelectorAll(
                        '.no-editable, .exercise-bottom-bar-opaque-box, [class*="opaque"]'
                    ).forEach(el => {
                        try {
                            el.style.pointerEvents = 'none';
                            el.style.zIndex = '-1';
                        } catch(e) {}
                    });
                }
            """)
        except Exception:
            pass

        self._scroll_into_view(locator)

        try:
            locator.click(timeout=timeout, force=True)
            return True
        except Exception as e:
            self.logger.debug(f"[SAFE_CLICK] force fail ({description}): {str(e)[:100]}")

        try:
            locator.evaluate("el => el.click()")
            return True
        except Exception as e:
            self.logger.debug(f"[SAFE_CLICK] JS fail ({description}): {str(e)[:100]}")

        return False

    def _safe_fill(self, locator, value, timeout=1500, description=""):
        try:
            if locator.count() == 0:
                return False
            self._scroll_into_view(locator)
            locator.fill(value, timeout=timeout)
            return True
        except Exception as e:
            self.logger.debug(f"[SAFE_FILL] fail ({description}): {str(e)[:100]}")
            return False

    def already_passed(self):
       
        if getattr(self, 'force_solve', False):
            return False
        try:
            score = self.get_result_score()
            return score >= self.PASS_LINE
        except Exception:
            return False

    def click_correction(self, settle=ACTION_SETTLE_SECONDS):
        for attempt in range(3):
            try:
                btn = self.page.locator("button.action-exercise-button.correct").first
                if btn.count() > 0 and btn.is_visible(timeout=1000):
                    if self._safe_click(btn, timeout=1200, description="correction"):
                        self.step("Clicked [Correction]")
                        time.sleep(settle)
                        return True
            except Exception as e:
                self.logger.error(f"Failed Correction attempt {attempt+1}: {str(e)}")
            time.sleep(0.5)
        return False

    def click_next(self):
        try:
            btn = self.page.locator("button.action-exercise-button.next").first
            if btn.count() > 0 and btn.is_visible(timeout=1000):
                if self._safe_click(btn, timeout=1200, description="next"):
                    self.step("Clicked [Next]")
                    time.sleep(EXERCISE_STEP_PAUSE_SECONDS)
                    return True
        except Exception as e:
            self.logger.error(f"Failed Next: {str(e)}")
        return False

    def click_solution(self, settle=ACTION_SETTLE_SECONDS):
        for attempt in range(3):
            try:
                btn = self.page.locator("button.solution").first
                if btn.count() > 0 and btn.is_visible(timeout=1000):
                    if self._safe_click(btn, timeout=1200, description="solution"):
                        self.step("Clicked [Solution]")
                        time.sleep(settle)
                        return True
            except Exception as e:
                self.logger.error(f"Failed Solution attempt {attempt+1}: {str(e)}")
            time.sleep(0.5)
        return False

    def click_repeat(self, settle=ACTION_SETTLE_SECONDS):
        for attempt in range(3):
            try:
                for sel in ["button.action-exercise-button.repeat",
                            "button.text-button-custom.repeat",
                            "button.repeat"]:
                    try:
                        btn = self.page.locator(sel).first
                        if btn.count() > 0 and btn.is_visible(timeout=800):
                            if self._safe_click(btn, timeout=1200, description="repeat"):
                                self.step("Clicked [Repeat]")
                                time.sleep(settle)
                                return True
                    except Exception:
                        continue
            except Exception as e:
                self.logger.error(f"Failed Repeat attempt {attempt+1}: {str(e)}")
            time.sleep(0.5)
        return False

    def click_start(self):
        try:
            btn = self.page.locator("button.start-exercise").first
            if btn.count() > 0 and btn.is_visible(timeout=1000):
                if self._safe_click(btn, timeout=1200, description="start"):
                    self.step("Clicked [Start]")
                    time.sleep(EXERCISE_STEP_PAUSE_SECONDS)
                    return True
        except Exception as e:
            self.logger.error(f"Failed Start: {str(e)}")
        return False

    def _neutralise_overlays(self):
        try:
            self.page.evaluate("""
                () => {
                    document.querySelectorAll(
                        '.exercise-bottom-bar-opaque-box, [class*="opaque"]'
                    ).forEach(el => {
                        try { el.style.pointerEvents = 'none'; } catch(e) {}
                    });
                }
            """)
        except Exception:
            pass

    def _restore_tiles_for_retry(self):
        try:
            if self.click_repeat():
                self.page.wait_for_timeout(250)
                return True
            if not self.click_solution():
                self.click_correction()
                self.page.wait_for_timeout(200)
                self.click_solution()
            self.page.wait_for_timeout(250)
            if self.click_repeat():
                self.page.wait_for_timeout(250)
                return True
        except Exception as e:
            self.logger.debug(f"[RESET_TILES] failed: {e}")
        self.logger.warning(
            "[RESET_TILES] could not reset exercise state (no Repeat button)")
        return False

    def has_start_button(self):
        try:
            btn = self.page.locator("button.start-exercise").first
            return btn.count() > 0 and btn.is_visible(timeout=800)
        except Exception:
            return False

    def _visible_locator(self, locator, timeout=600):
        try:
            if locator.count() == 0:
                return None
            first = locator.first
            if not first.is_visible(timeout=timeout):
                return None
            return first
        except Exception:
            return None

    def get_result_score(self):
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline:
                try:
                    badge = self._visible_locator(self.page.locator(".result-badge-text"))
                    if badge is not None:
                        score = badge.get_attribute("data-result")
                        if score is not None:
                            try:
                                value = int(float(str(score).strip()))
                                if 0 <= value <= 100:
                                    return value
                            except (ValueError, TypeError):
                                pass
                    
                    if self._page_is_answered():
                        break
                except Exception:
                    pass
                time.sleep(0.15)

            body_text = self.page.locator("body").inner_text(timeout=800)
            if any(t in body_text for t in ["Very good!", "Excellent",
                                            "Well done", "Perfect",
                                            "ดีมาก", "ยอดเยี่ยม", "ยอดมาก"]):
                return 100
            if any(t in body_text for t in ["Try again", "ลองอีกครั้ง",
                                            "You can do better"]):
                return 0

            bars = []
            try:
                candidates = self.page.locator(
                    ".score-bar, .result-bar, [class*='bar-']").all()
                for bar in candidates:
                    if self._visible_locator(bar, 150) is not None:
                        bars.append(bar)
            except Exception:
                bars = []
            if bars:
                green_count = 0
                for bar in bars:
                    cls = (bar.get_attribute("class") or "").lower()
                    if "green" in cls or "success" in cls:
                        green_count += 1
                if green_count == len(bars):
                    return 100
                elif green_count > 0:
                    return int((green_count / len(bars)) * 100)

            try:
                if self.page.evaluate(_GREEN_MARKER_JS):
                    return 100
            except Exception:
                pass

            return 0
        except Exception:
            return 0

    def wait_for_page_ready(self):
        try:
            self.page.wait_for_selector(".exercise, .video", state="visible", timeout=5000)
            time.sleep(0.2)
            return True
        except Exception:
            return False

    def precise_drag(self, source, target):
        for attempt in range(2):
            try:
                self._neutralise_overlays()
                try:
                    source.scroll_into_view_if_needed(timeout=1000)
                except Exception:
                    pass
                try:
                    target.scroll_into_view_if_needed(timeout=1000)
                except Exception:
                    pass
                time.sleep(DRAG_CLICK_SETTLE)
                source_box = source.bounding_box()
                target_box = target.bounding_box()
                if not source_box or not target_box:
                    return False
                sx = source_box['x'] + source_box['width'] / 2
                sy = source_box['y'] + source_box['height'] / 2
                tx = target_box['x'] + target_box['width'] / 2
                ty = target_box['y'] + target_box['height'] / 2
                self.page.mouse.move(sx, sy)
                time.sleep(DRAG_CLICK_SETTLE)
                self.page.mouse.down()
                time.sleep(DRAG_CLICK_SETTLE)
                self.page.mouse.move(sx + 5, sy + 5)
                time.sleep(DRAG_SYNC_INTERVAL)
                self.page.mouse.move(sx + 10, sy + 10)
                time.sleep(DRAG_SYNC_INTERVAL)
                steps = 12
                for step_i in range(1, steps + 1):
                    x = sx + (tx - sx) * step_i / steps
                    y = sy + (ty - sy) * step_i / steps
                    self.page.mouse.move(x, y)
                    time.sleep(DRAG_SYNC_INTERVAL)
                self.page.mouse.move(tx, ty)
                time.sleep(DRAG_PLACE_SETTLE)
                self.page.mouse.move(tx + 1, ty)
                time.sleep(DRAG_SYNC_INTERVAL)
                self.page.mouse.move(tx, ty)
                time.sleep(DRAG_SYNC_INTERVAL)
                self.page.mouse.up()
                time.sleep(DRAG_PLACE_SETTLE)
                return True
            except Exception as e:
                self.logger.error(f"Drag error (attempt {attempt+1}): {str(e)}")
                try:
                    self.page.mouse.up()
                except Exception:
                    pass
                time.sleep(FAST_ACTION_SETTLE_SECONDS)
        return False

    def fast_drag(self, source, target):
        try:
            self._neutralise_overlays()
            source.scroll_into_view_if_needed(timeout=1500)
            target.scroll_into_view_if_needed(timeout=1500)
            source_box = source.bounding_box()
            target_box = target.bounding_box()
            if not source_box or not target_box:
                return False
            sx = source_box["x"] + source_box["width"] / 2
            sy = source_box["y"] + source_box["height"] / 2
            tx = target_box["x"] + target_box["width"] / 2
            ty = target_box["y"] + target_box["height"] / 2
            self.page.mouse.move(sx, sy)
            self.page.mouse.down()
            self.page.mouse.move((sx + tx) / 2, (sy + ty) / 2)
            self.page.mouse.move(tx, ty)
            self.page.mouse.up()
            return True
        except Exception as e:
            self.logger.debug(f"[FAST_DRAG] failed: {str(e)[:120]}")
            try:
                self.page.mouse.up()
            except Exception:
                pass
            return False

    def _handle_audio_setup_modal(self):
        try:
            return dismiss_audio_setup_modal(
                self.page, self.logger, self.step
            )
        except Exception as e:
            self.logger.debug(f"[AUDIO] Error checking/handling audio setup modal: {e}")
            return False

class DragDropSolver(BaseSolver):
    MAX_ITERATIONS = 3
    EMPTY_TILE = "__SPEEXX_EMPTY_DRAG_TILE__"

    @staticmethod
    def _normalise(text):
        return " ".join((text or "").split())

    @classmethod
    def _answer_text(cls, text):
        return cls.EMPTY_TILE if not cls._normalise(text) else cls._normalise(text)

    @classmethod
    def _is_empty_answer(cls, answer):
        return answer == cls.EMPTY_TILE

    def _is_table_layout(self):
        try:
            exercise = self.page.locator(".exercise").first
            if exercise.count() > 0:
                cls = exercise.get_attribute("class") or ""
                if "type-drag-drop-table" in cls or "layout-table" in cls or "layout-grid-duo" in cls:
                    return True
            items = self.page.locator(".exercise-items .item.line-breaks").all()
            if len(items) >= 2:
                placeholder_counts = []
                for item in items:
                    count = item.locator(".drag-drop-placeholder").count()
                    if count > 0:
                        placeholder_counts.append(count)
                if len(set(placeholder_counts)) > 1:
                    return True
        except Exception:
            pass
        return False

    def _slot_answers(self):
        try:
            js_code = """
            () => {
                const EM = "__SPEEXX_EMPTY_DRAG_TILE__";
                // ใช้ placeholder ทั้งหมด (รวม hidden) เพื่อรักษาลำดับช่องที่ถูกต้อง
                const slots = Array.from(document.querySelectorAll('.exercise-items .drag-drop-placeholder'));
                
                if (slots.length > 0) {
                    const answers = [];
                    for (const slot of slots) {
                        // หา tile ที่เกี่ยวข้องกับ placeholder นี้
                        // tile อาจเป็น child, previousSibling, หรือ nextSibling
                        let tile = slot.querySelector('.drag-drop, [data-drag-drop-id]');
                        if (!tile) {
                            let prev = slot.previousElementSibling;
                            if (prev && prev.matches('.drag-drop, [data-drag-drop-id]')) tile = prev;
                        }
                        if (!tile) {
                            let next = slot.nextElementSibling;
                            if (next && next.matches('.drag-drop, [data-drag-drop-id]')) tile = next;
                        }
                        // ถ้ายังไม่เจอ ลองหาใน parent .item
                        if (!tile) {
                            const item = slot.closest('.item');
                            if (item) {
                                const tiles = item.querySelectorAll('.drag-drop, [data-drag-drop-id]');
                                // หา tile ที่ไม่ได้ถูกซ่อน (ไม่ใช่ placeholder ที่ hidden)
                                for (const t of tiles) {
                                    const tStyle = window.getComputedStyle(t);
                                    if (tStyle.display !== 'none' && tStyle.visibility !== 'hidden') {
                                        // ตรวจสอบว่า tile นี้อยู่ใกล้ placeholder นี้
                                        const slotRect = slot.getBoundingClientRect();
                                        const tileRect = t.getBoundingClientRect();
                                        // ถ้าอยู่ใน item เดียวกันและตำแหน่งใกล้เคียง
                                        if (Math.abs(slotRect.left - tileRect.left) < 100 && Math.abs(slotRect.top - tileRect.top) < 100) {
                                            tile = t;
                                            break;
                                        }
                                    }
                                }
                            }
                        }
                        if (tile) {
                            let txt = (tile.innerText || '').trim();
                            answers.push(txt === '' ? EM : txt);
                        } else {
                            answers.push(EM);
                        }
                    }
                    return answers;
                }
                
                // Fallback: หา tiles ทั้งหมดใน exercise-items ที่มองเห็นได้
                const tiles = Array.from(document.querySelectorAll('.exercise-items .drag-drop, .exercise-items [data-drag-drop-id]'))
                    .filter(t => {
                        const style = window.getComputedStyle(t);
                        return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0';
                    });
                return tiles.map(t => {
                    let txt = (t.innerText || '').trim();
                    return txt === '' ? EM : txt;
                });
            }
            """
            return self.page.evaluate(js_code)
        except Exception:
            return []

    def _slot_answers_table_layout(self):
        try:
            js_code = """
            () => {
                const EM = "__SPEEXX_EMPTY_DRAG_TILE__";
                const items = Array.from(document.querySelectorAll('.exercise-items .item.line-breaks'));
                const all_answers = [];
                
                for (const item of items) {
                    const slots = Array.from(item.querySelectorAll('.drag-drop-placeholder'));
                    
                    for (const slot of slots) {
                        let tile = slot.querySelector('.drag-drop, [data-drag-drop-id]');
                        if (!tile) {
                            let prev = slot.previousElementSibling;
                            if (prev && prev.matches('.drag-drop, [data-drag-drop-id]')) tile = prev;
                        }
                        if (!tile) {
                            let next = slot.nextElementSibling;
                            if (next && next.matches('.drag-drop, [data-drag-drop-id]')) tile = next;
                        }
                        if (!tile) {
                            const tiles = item.querySelectorAll('.drag-drop, [data-drag-drop-id]');
                            for (const t of tiles) {
                                const tStyle = window.getComputedStyle(t);
                                if (tStyle.display !== 'none' && tStyle.visibility !== 'hidden') {
                                    const slotRect = slot.getBoundingClientRect();
                                    const tileRect = t.getBoundingClientRect()
                                    if (Math.abs(slotRect.left - tileRect.left) < 100 && Math.abs(slotRect.top - tileRect.top) < 100) {
                                        tile = t;
                                        break;
                                    }
                                }
                            }
                        }
                        if (tile) {
                            let txt = (tile.innerText || '').trim();
                            all_answers.push(txt === '' ? EM : txt);
                        } else {
                            all_answers.push(EM);
                        }
                    }
                }
                return all_answers;
            }
            """
            return self.page.evaluate(js_code)
        except Exception:
            return []

    SLOT_SELECTOR = ".drag-drop-placeholder"
    TILE_SELECTOR = ".draggable-container .drag-drop, .draggable-container [data-drag-drop-id]"

    def _source_tiles(self):
        tiles = self.page.locator(self.TILE_SELECTOR).all()
        visible = []
        for tile in tiles:
            try:
                if tile.is_visible():
                    visible.append(tile)
            except Exception:
                continue
        return visible

    def _slot_locators(self):
        try:
            locs = self.page.locator(self.SLOT_SELECTOR).all()
            return locs
        except Exception:
            return []

    def _tile_text(self, tile):
        try:
            return tile.inner_text().strip()
        except Exception:
            return ""

    def _is_slot_filled(self, slot):
        try:
            tile = slot.locator(".drag-drop, [data-drag-drop-id], .picture-gap").first
            if tile.count() > 0:
                return True
            prev = slot.locator("xpath=preceding-sibling::*[1][self::span[contains(@class,'drag-drop') or @data-drag-drop-id]]").first
            if prev.count() > 0:
                style = prev.evaluate("el => window.getComputedStyle(el).display")
                if style != "none":
                    return True
            next_sib = slot.locator("xpath=following-sibling::*[1][self::span[contains(@class,'drag-drop') or @data-drag-drop-id]]").first
            if next_sib.count() > 0:
                style = next_sib.evaluate("el => window.getComputedStyle(el).display")
                if style != "none":
                    return True
            return False
        except Exception:
            return False

    def _find_source_tile(self, wanted):
        fallback = None
        for tile in self._source_tiles():
            text = self._answer_text(self._tile_text(tile))
            if text == wanted:
                return tile
            if fallback is None and text.casefold() == wanted.casefold():
                fallback = tile
        return fallback

    def _drag_tile_to_slot(self, source, slot):
        self._neutralise_overlays()
        try:
            source.scroll_into_view_if_needed(timeout=1500)
            slot.scroll_into_view_if_needed(timeout=1500)
            self.page.wait_for_timeout(30)
            self.precise_drag(source, slot)
            return True
        except Exception as e:
            self.logger.debug(f"[DND] drag failed: {e}")
            return False

    def _restore_by_group_id(self):
        if self._is_table_layout():
            self.logger.info("[DND] Table layout detected, skipping group-id fallback")
            return []

        tiles = self._source_tiles()
        if not tiles:
            return []

        groups = {}
        for tile in tiles:
            gid = tile.get_attribute("data-group-id")
            if not gid:
                return [] 
            text = self._answer_text(self._tile_text(tile))
            groups.setdefault(gid, []).append(text)

        if not groups:
            return []

        items = self.page.locator(".exercise-items .item").all()
        if not items:
            return []

        placeholder_rows = []
        for item in items:
            placeholders = item.locator(".drag-drop-placeholder").all()
            if placeholders:
                placeholder_rows.append(placeholders)

        if not placeholder_rows:
            return []

        column_count = len(placeholder_rows[0])
        if column_count == 0:
            return []
        
        for row in placeholder_rows:
            if len(row) != column_count:
                self.logger.warning("[DND] Non-uniform grid detected, group-id fallback may not work")
                return []

        columns = {}
        for gid, texts in groups.items():
            try:
                col_idx = int(gid) - 1
            except (ValueError, TypeError):
                return []
            if 0 <= col_idx < column_count:
                columns[col_idx] = list(texts)

        answers = []
        for row in placeholder_rows:
            for col_idx in range(len(row)):
                col_tiles = columns.get(col_idx, [])
                if col_tiles:
                    answers.append(col_tiles.pop(0))
                else:
                    answers.append(self.EMPTY_TILE)

        return answers

    def _restore_solution(self):
        self.click_correction()
        time.sleep(0.6)

        ok = False
        for _ in range(3):
            if self.click_solution():
                ok = True
                break
            time.sleep(0.6)

        if ok:
            self.page.wait_for_timeout(500)
            answers = self._slot_answers()
            self.click_repeat()
            self.page.wait_for_timeout(500)
            if answers and any(answers):
                return answers

        self.logger.info("[DND] No solution button, using data-group-id fallback")
        self.click_repeat()
        self.page.wait_for_timeout(500)
        return self._restore_by_group_id()

    def _detect_reorder_mode(self):
        try:
            source_tiles = self.page.locator(".draggable-container .drag-drop, .draggable-container [data-drag-drop-id]").all()
            visible_sources = [t for t in source_tiles if t.is_visible(timeout=200)]
            
            slot_tiles = self.page.locator(".exercise-items .drag-drop-placeholder .drag-drop, .exercise-items .drag-drop-placeholder [data-drag-drop-id], .exercise-items .drag-drop-placeholder .picture-gap").all()
            
            self.logger.info(f"[DND] Detection: {len(visible_sources)} visible source tiles, {len(slot_tiles)} tiles in slots")
            return len(visible_sources) == 0 and len(slot_tiles) > 0
        except Exception as e:
            self.logger.debug(f"[DND] Reorder detection failed: {e}")
            return False

    def _reorder_slots_by_answer(self, answers):
        try:
            slots = self.page.locator(".exercise-items .drag-drop-placeholder").all()
            if not slots:
                return 0
            
            moved = 0
            current_tiles = []
            for slot in slots:
                tile = slot.locator(".drag-drop, [data-drag-drop-id], .picture-gap").first
                if tile.count() > 0:
                    text = self._answer_text(tile.inner_text())
                    current_tiles.append({"slot_idx": len(current_tiles), "tile": tile, "text": text})
                else:
                    current_tiles.append({"slot_idx": len(current_tiles), "tile": None, "text": None})
            
            for target_idx, wanted_text in enumerate(answers):
                if not wanted_text or wanted_text == self.EMPTY_TILE:
                    continue
                if target_idx >= len(current_tiles):
                    continue
                
                source_slot_info = None
                for info in current_tiles:
                    if info["text"] and self._answer_text(info["text"]).casefold() == wanted_text.casefold():
                        source_slot_info = info
                        break
                
                if not source_slot_info:
                    continue
                
                source_idx = source_slot_info["slot_idx"]
                if source_idx == target_idx:
                    continue
                
                source_slot = slots[source_idx]
                target_slot = slots[target_idx]
                
                source_tile = source_slot.locator(".drag-drop, [data-drag-drop-id], .picture-gap").first
                if source_tile.count() == 0:
                    continue
                
                try:
                    self._safe_click(target_slot, timeout=2000, description="dnd-target-slot")
                    time.sleep(0.1)
                    self._safe_click(source_tile, timeout=2000, description="dnd-source-tile")
                    time.sleep(0.3)
                    
                    new_text = self._answer_text(target_slot.locator(".drag-drop, [data-drag-drop-id], .picture-gap").first.inner_text())
                    if new_text.casefold() == wanted_text.casefold():
                        moved += 1
                except Exception:
                    pass
            
            return moved
        except Exception as e:
            self.logger.error(f"[DND] Reorder slots error: {e}")
            return 0

    def _fill_slots(self, answers):
        moved = 0
        click_placed = 0
        drag_placed = 0
        
        slots = self._slot_locators()
        if not slots:
            return 0

        first_empty_slot = None
        for slot in slots:
            if not self._is_slot_filled(slot):
                first_empty_slot = slot
                break
        if first_empty_slot:
            try:
                self._safe_click(first_empty_slot, timeout=2000, description="dnd-first-slot")
                self.page.wait_for_timeout(200)
            except Exception:
                pass

        slot_index = 0
        for index, answer in enumerate(answers):
            wanted = self._answer_text(answer)
            if not wanted or wanted == self.EMPTY_TILE:
                continue
            source = self._find_source_tile(wanted)
            
            if not source:
                continue
            
            target_slot = None
            while slot_index < len(slots):
                if not self._is_slot_filled(slots[slot_index]):
                    target_slot = slots[slot_index]
                    slot_index += 1
                    break
                slot_index += 1
            
            if not target_slot:
                self.logger.warning(f"[DND] No empty slot for answer: {wanted}")
                continue
            
            placed = False
            before_count = len(self._source_tiles())
            
            try:
                self._safe_click(source, timeout=1500, description="dnd-direct-tile")
                self.page.wait_for_timeout(100)
                after_count = len(self._source_tiles())
                if after_count < before_count or not self._find_source_tile(wanted):
                    placed = True
                    click_placed += 1
            except Exception:
                pass
            
            if not placed:
                try:
                    before_count = len(self._source_tiles())
                    self._safe_click(target_slot, timeout=1500, description="dnd-target-slot")
                    self.page.wait_for_timeout(100)
                    
                    source = self._find_source_tile(wanted)
                    if source:
                        self._safe_click(source, timeout=1500, description="dnd-target-tile")
                        self.page.wait_for_timeout(100)
                        
                        after_count = len(self._source_tiles())
                        if after_count < before_count or not self._find_source_tile(wanted):
                            placed = True
                            click_placed += 1
                except Exception:
                    pass
            
            if not placed:
                source = self._find_source_tile(wanted)
                if source:
                    try:
                        before_count = len(self._source_tiles())
                        self.precise_drag(source, target_slot)
                        self.page.wait_for_timeout(150)
                        after_count = len(self._source_tiles())
                        if after_count < before_count or not self._find_source_tile(wanted):
                            placed = True
                            drag_placed += 1
                    except Exception:
                        pass

            if placed:
                moved += 1
            
            self.page.wait_for_timeout(80)

        self.logger.info(
            "[DND] Placement methods: %s by click, %s by drag fallback",
            click_placed, drag_placed)
        return moved

    def solve(self):
        self.logger.info("=== Drag & Drop Solver Started ===")
        self.start_timer()

        if self.already_passed():
            self.logger.info("[DND] Already passed (score >= 51)")
            return True

        try:
            if not self.wait_for_page_ready():
                return False

            answers = self._restore_solution()
            if not answers or not any(answers):
                self.logger.error("Could not extract drag/drop solution mapping")
                return False
            extracted_count = sum(
                1 for answer in answers if answer is not None and answer != ""
            )
            self.step(f"Extracted {extracted_count} drag/drop answers")

            if self.answer_db is not None:
                question_text = self._extract_question_text()
                self.answer_db[question_text] = {
                    "type": "drag-drop",
                    "answers": answers,
                    "timestamp": datetime.now().isoformat()
                }
                self.step(f"Saved drag/drop answers to DB")

            for attempt in range(self.MAX_ITERATIONS):
                if attempt > 0 and not self._restore_tiles_for_retry():
                    self.logger.warning(
                        "[DND] Cannot reset tiles for retry — stop trying")
                    break

                is_reorder_mode = self._detect_reorder_mode()
                self.logger.info(
                    "[DND] Exercise mode: %s",
                    "reorder-within-slots" if is_reorder_mode else "drag-from-source")

                if is_reorder_mode:
                    moved = self._reorder_slots_by_answer(answers)
                    self.step(f"Reordered {moved} tiles (attempt {attempt + 1})")
                else:
                    moved = self._fill_slots(answers)
                    self.step(f"Placed {moved} drag/drop tiles (attempt {attempt + 1})")

                self.click_correction()
                self.page.wait_for_timeout(300)
                score = self.get_result_score()
                if score >= 100:
                    self.logger.info("Drag/drop completed in %s", self.stop_timer())
                    return True
                self.logger.info(
                    "[DND] Score after attempt %s: %s", attempt + 1, score)

            return self.get_result_score() >= 100
        except Exception as e:
            self.logger.error(f"Drag/drop error: {e}")
            return False

class VideoSolver(BaseSolver):
    def solve(self):
        self.logger.info("=== Video Solver Started ===")
        self.start_timer()
        try:
            result = self.page.evaluate("""
                () => {
                    const videos = document.querySelectorAll('video');
                    if (videos.length === 0) return 'no-video';
                    let acted = false;
                    videos.forEach(v => {
                        try {
                            v.muted = true;
                            v.playbackRate = 16;
                            if (v.duration && !isNaN(v.duration) && v.duration > 0) {
                                v.currentTime = Math.max(0, v.duration - 0.3);
                                acted = true;
                            }
                            v.play().catch(e => {});
                        } catch(e) {}
                    });
                    return acted ? 'seeked-and-played' : 'playing';
                }
            """)
            self.step(f"Video action: {result}")
            time.sleep(0.8)
            for i in range(20):
                try:
                    state = self.page.evaluate("""
                        () => {
                            const v = document.querySelector('video');
                            if (!v) return {ended: true, currentTime: 0, duration: 0};
                            return {
                                ended: v.ended || false,
                                currentTime: v.currentTime || 0,
                                duration: v.duration || 0,
                            };
                        }
                    """)
                    if state.get("ended", False):
                        break
                    if state.get("duration", 0) > 0 and state.get("currentTime", 0) >= state.get("duration", 0) - 0.5:
                        break
                except Exception:
                    pass
                time.sleep(0.2)
            time.sleep(0.5)
            for i in range(10):
                next_btn = self.page.locator("button.action-exercise-button.next").first
                if next_btn.count() > 0:
                    try:
                        if next_btn.is_enabled():
                            break
                    except Exception:
                        pass
                time.sleep(0.2)
            elapsed = self.stop_timer()
            self.logger.info(f"Video done. Time: {elapsed}")
            return True
        except Exception as e:
            self.logger.error(f"Video error: {str(e)}")
            return False

class AnswerSolver(BaseSolver):
    MAX_ITERATIONS = 8

    def solve(self):
        self.logger.info("=== Answer Solver ===")
        self.start_timer()
        if not self.wait_for_page_ready():
            return False
        if self.has_start_button():
            self.click_start()
            time.sleep(EXERCISE_STEP_PAUSE_SECONDS)
        self.click_correction(settle=ACTION_SETTLE_SECONDS)
        time.sleep(EXERCISE_STEP_PAUSE_SECONDS)
        if not self.click_solution(settle=ACTION_SETTLE_SECONDS):
            return False
        time.sleep(ACTION_SETTLE_SECONDS)
        correct_answers = self.extract_answers_from_solution()
        if not correct_answers or all(a == "" for a in correct_answers):
            return False
        self.step(f"Extracted {len(correct_answers)} answers: {correct_answers}")

        if self.answer_db is not None:
            question_text = self._extract_question_text()
            self.answer_db[question_text] = {
                "type": "answer",
                "answers": correct_answers,
                "timestamp": datetime.now().isoformat()
            }
            self.step(f"Saved answers to DB")

        self.click_repeat(settle=ACTION_SETTLE_SECONDS)
        time.sleep(ACTION_SETTLE_SECONDS)
        if self.has_start_button():
            self.click_start()
            time.sleep(EXERCISE_STEP_PAUSE_SECONDS)

        if self._fill_blanks_js(correct_answers):
            self.step("Fill blanks via JS (fast path)")
            time.sleep(0.5)
            self.click_correction(settle=ACTION_SETTLE_SECONDS)
            time.sleep(EXERCISE_STEP_PAUSE_SECONDS)
            self.page.wait_for_timeout(500)
            if self.get_result_score() >= 51:
                return True

        for iteration in range(self.MAX_ITERATIONS):
            score = self.get_result_score()
            self.logger.info(f"--- Iteration {iteration+1}/{self.MAX_ITERATIONS} | Score: {score} ---")
            if score >= 51:
                return True
            if self.has_start_button():
                self.click_start()
                time.sleep(EXERCISE_STEP_PAUSE_SECONDS)
            self.fill_blanks(correct_answers)
            time.sleep(EXERCISE_STEP_PAUSE_SECONDS)
            self.click_correction(settle=ACTION_SETTLE_SECONDS)
            time.sleep(EXERCISE_STEP_PAUSE_SECONDS)
        return self.get_result_score() >= 61

    def extract_answers_from_solution(self):
        answers = []
        try:
            for inp in self.page.locator("input.answer").all():
                answers.append(inp.input_value().strip())
        except Exception as e:
            self.logger.error(f"Extract error: {str(e)}")
        return answers

    def _fill_blanks_js(self, answers):
        try:
            result = self.page.evaluate("""
                (answers) => {
                    const inputs = document.querySelectorAll("input.answer");
                    let success = false;
                    answers.forEach((ans, i) => {
                        if (i >= inputs.length || !ans) return;
                        const inp = inputs[i];
                        inp.focus();
                        inp.value = ans;
                        inp.dispatchEvent(new Event('input', {bubbles: true}));
                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                        inp.dispatchEvent(new Event('blur', {bubbles: true}));
                        success = true;
                    });
                    return success;
                }
            """, answers)
            return result
        except Exception as e:
            self.logger.debug(f"[ANSWER] JS fast path failed: {e}")
            return False

    def fill_blanks(self, answers):
        try:
            inputs = self.page.locator("input.answer").all()
            if len(inputs) == 0:
                return
            for i, ans in enumerate(answers):
                if i >= len(inputs) or not ans:
                    continue
                try:
                    self._safe_fill(inputs[i], ans, description=f"fill_blank_{i}")
                    time.sleep(0.05)
                except Exception:
                    pass
        except Exception:
            pass

class ScrambledSentenceSolver(BaseSolver):
    MAX_ITERATIONS = 3
    FAST_SETTLE = FAST_ACTION_SETTLE_SECONDS

    @staticmethod
    def _clean_word(word):
        return str(word).replace("\xa0", " ").strip()

    @classmethod
    def _normalise_sentences(cls, raw):
        sentences = []
        for entry in raw or []:
            if isinstance(entry, dict):
                words = entry.get("value")
                if words is None:
                    words = entry.get("words")
                if not isinstance(words, (list, tuple)):
                    continue
            elif isinstance(entry, (list, tuple)):
                words = entry
            else:
                continue
            cleaned = [cls._clean_word(w) for w in words]
            cleaned = [w for w in cleaned if w]
            if cleaned:
                sentences.append(cleaned)
        return sentences

    def _block_words(self, scrambled_element):
        try:
            words = []
            for block in scrambled_element.locator(".scrambled-block").all():
                text = self._clean_word(block.inner_text())
                if text:
                    words.append(text)
            return words
        except Exception:
            return []

    def _current_sentences(self):
        sentences = []
        try:
            for item in self.page.locator(".exercise-items .item").all():
                scrambled = item.locator(".scrambled-sentence").first
                if scrambled.count() == 0:
                    continue
                sentences.append(self._block_words(scrambled))
        except Exception:
            return []
        return sentences

    def _sentences_match_target(self, target_sentences):
        current = self._current_sentences()
        if len(current) != len(target_sentences):
            return False
        return all(now == want for now, want in zip(current, target_sentences))

    def _cached_targets(self):
        try:
            question = self._extract_question_text()
            exact = self.answer_db.get(question)
            if isinstance(exact, dict) and exact.get("type") == "scrambled-sentence":
                return self._normalise_sentences(exact.get("answers"))

            stable = " | ".join(question.split(" | ")[:2])
            for key, entry in self.answer_db.items():
                if not isinstance(entry, dict) or entry.get("type") != "scrambled-sentence":
                    continue
                if " | ".join(str(key).split(" | ")[:2]) == stable:
                    return self._normalise_sentences(entry.get("answers"))
        except Exception as e:
            self.logger.debug(f"[CACHE] scrambled lookup failed: {str(e)[:120]}")
        return None

    def _click_reorder(self, scrambled_element, source_index, target_index):
        try:
            blocks = scrambled_element.locator(".scrambled-block").all()
            if source_index >= len(blocks) or target_index >= len(blocks):
                return False

            blocks[target_index].click(timeout=2000)
            time.sleep(0.08)
            blocks[source_index].click(timeout=2000)
            time.sleep(0.25)

            after = [b.inner_text().strip()
                     for b in scrambled_element.locator(".scrambled-block").all()]
            return len(after) == len(blocks)
        except Exception as e:
            self.logger.debug(f"click reorder failed: {str(e)[:120]}")
            return False

    def _dom_reorder(self, scrambled_element, target):
        try:
            result = scrambled_element.evaluate(
                """(el, wanted) => {
                    const blocks = Array.from(el.querySelectorAll(':scope > .scrambled-block'));
                    const used = new Set();
                    const normal = value => (value || '').replace(/\\u00a0/g, ' ').trim();
                    const ordered = [];
                    for (const word of wanted) {
                        const idx = blocks.findIndex((block, i) =>
                            !used.has(i) && normal(block.innerText) === normal(word));
                        if (idx < 0) return false;
                        used.add(idx);
                        ordered.push(blocks[idx]);
                    }
                    for (const block of ordered) el.appendChild(block);
                    if (window.jQuery) {
                        try { window.jQuery(el).sortable('refresh'); } catch (e) {}
                        window.jQuery(el).trigger('sort');
                        window.jQuery(el).trigger('sortupdate');
                        window.jQuery(el).trigger('change');
                    }
                    el.dispatchEvent(new Event('sortupdate', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    return Array.from(el.querySelectorAll(':scope > .scrambled-block'))
                        .map(block => normal(block.innerText));
                }""",
                target,
            )
            return result == target
        except Exception as e:
            self.logger.debug(f"DOM reorder failed: {str(e)[:120]}")
            return False

    def _extract_ordered_sentences_from_ids(self):
        sentences = []
        expected = 0
        try:
            for item in self.page.locator(".exercise-items .item").all():
                scrambled = item.locator(".scrambled-sentence").first
                if scrambled.count() == 0:
                    continue
                expected += 1
                numbered = []
                for block in scrambled.locator(".scrambled-block").all():
                    block_id = block.get_attribute("data-scrambled-block-id") or ""
                    match = re.search(r"(\d+)\s*$", block_id)
                    text = block.inner_text().strip()
                    if not match or not text:
                        numbered = []
                        break
                    numbered.append((int(match.group(1)), text))
                if not numbered:
                    return []
                numbered.sort(key=lambda pair: pair[0])
                sentences.append([text for _, text in numbered])
        except Exception as e:
            self.logger.debug(f"[SCRAMBLED] id-order extraction failed: {e}")
            return []
        return sentences if expected and len(sentences) == expected else []

    def solve(self):
        self.logger.info("=== Scrambled Sentence Solver ===")
        self.start_timer()
        if not self.wait_for_page_ready():
            return False

        target_sentences = None
        if getattr(self, "prefer_db", True):
            cached = self._cached_targets()
            if cached:
                if len(cached) == len(self._current_sentences()):
                    target_sentences = cached
                    self.step("[DB] Using cached scrambled-sentence answers")
                else:
                    self.logger.info(
                        "[DB] Cached scrambled answers cover %s sentences but page "
                        "has %s — extracting from the page instead",
                        len(cached), len(self._current_sentences()))

        if not target_sentences:
            self.click_correction()
            if self.click_solution():
                target_sentences = self._normalise_sentences(
                    self.extract_solution_sentences())

        if not target_sentences:
            target_sentences = self._normalise_sentences(
                self._extract_ordered_sentences_from_ids())
            if target_sentences:
                self.step("[SCRAMBLED] Using block-id order fallback")

        if not target_sentences:
            self.logger.warning("Failed to extract solution. Using heuristic fallback.")
            return self._solve_heuristic()

        if self.answer_db is not None:
            question_text = self._extract_question_text()
            self.answer_db[question_text] = {
                "type": "scrambled-sentence",
                "answers": target_sentences,
                "timestamp": datetime.now().isoformat()
            }
            self.step("Saved scrambled sentences to DB")

        self.click_repeat(settle=self.FAST_SETTLE)
        time.sleep(self.FAST_SETTLE)
        self.wait_for_page_ready()

        max_iter = self.MAX_ITERATIONS
        best_score = 0
        for iteration in range(max_iter):
            score = self.get_result_score()
            best_score = max(best_score, score)
            self.logger.info(
                f"--- Iteration {iteration+1}/{max_iter} | Score: {score} ---")
            if score >= 51:
                return True

            if self._sentences_match_target(target_sentences):
                self.step("Sentences arranged but score low — re-dragging")
                self.apply_all_targets(target_sentences, force=True)
            else:
                self.apply_all_targets(target_sentences)
            time.sleep(self.FAST_SETTLE)

            self.click_correction(settle=self.FAST_SETTLE)
            time.sleep(self.FAST_SETTLE)

            score = self.get_result_score()
            best_score = max(best_score, score)
            self.logger.info(
                "[SCRAMBLED] After correction: score=%s | in target order=%s",
                score, self._sentences_match_target(target_sentences))
            if score >= 51:
                return True

        final_score = self.get_result_score()
        self.logger.info(
            f"[SCRAMBLED] Solver finished. Final score check: {final_score}")
        return final_score >= 51

    def extract_solution_sentences(self):
        sentences = []
        try:
            items = self.page.locator(".exercise-items .item").all()
            for item in items:
                scrambled = item.locator(".scrambled-sentence").first
                if scrambled.count() == 0:
                    continue
                words = []
                for block in scrambled.locator(".scrambled-block").all():
                    text = self._clean_word(block.inner_text())
                    if text:
                        words.append(text)
                if words:
                    sentences.append(words)
        except Exception:
            pass
        return sentences

    def apply_all_targets(self, target_sentences, force=False):
        scrambled_items = []
        try:
            for item in self.page.locator(".exercise-items .item").all():
                scrambled = item.locator(".scrambled-sentence").first
                if scrambled.count() > 0:
                    scrambled_items.append(scrambled)
        except Exception:
            return

        for idx, target in enumerate(target_sentences):
            if idx >= len(scrambled_items):
                break
            self.apply_target(scrambled_items[idx], list(target), force=force)

    def apply_target(self, scrambled_element, target, force=False):
        moved = False
        try:
            current = self._block_words(scrambled_element)
            if current == target and not force:
                return False

            if force:
                blocks = scrambled_element.locator(".scrambled-block").all()
                if len(blocks) >= 2 and current == target:
                    self.precise_drag(blocks[-1], blocks[0])
                    time.sleep(0.25)

            if not force and self._dom_reorder(scrambled_element, target):
                return True

            drag_gave_up = False
            for iteration in range(len(target) * 3):
                blocks = scrambled_element.locator(".scrambled-block").all()
                current = self._block_words(scrambled_element)
                if len(current) != len(target) or len(blocks) != len(target):
                    return moved
                mismatch = -1
                for i in range(len(target)):
                    if current[i] != target[i]:
                        mismatch = i
                        break
                if mismatch == -1:
                    return moved
                target_word = target[mismatch]
                source_idx = -1
                for j in range(mismatch + 1, len(current)):
                    if current[j] == target_word:
                        source_idx = j
                        break
                if source_idx == -1:
                    for j in range(len(current)):
                        if j != mismatch and current[j] == target_word:
                            source_idx = j
                            break
                if source_idx == -1:
                    target[mismatch] = current[mismatch]
                    continue
                drag_ok = False
                if not drag_gave_up:
                    for drag_attempt in range(2):
                        before = self._block_words(scrambled_element)
                        self.precise_drag(blocks[source_idx], blocks[mismatch])
                        time.sleep(0.25)
                        after = self._block_words(scrambled_element)
                        if after != before:
                            drag_ok = True
                            break
                if not drag_ok:
                    drag_gave_up = True
                    if self._click_reorder(scrambled_element, source_idx, mismatch):
                        after = self._block_words(scrambled_element)
                        drag_ok = after != current
                if drag_ok:
                    moved = True
                    time.sleep(0.12)
                else:
                    if self._dom_reorder(scrambled_element, target):
                        moved = True
                        return moved
                    target[mismatch] = current[mismatch]
        except Exception as e:
            self.logger.error(f"apply_target error: {str(e)}")
        return moved

    def _solve_heuristic(self):
        last_scores = []
        stuck_level = 0
        for iteration in range(30):
            score = self.get_result_score()
            if score >= 51:
                return True
            last_scores.append(score)
            if len(last_scores) > 3:
                last_scores.pop(0)
            if len(last_scores) >= 3 and len(set(last_scores)) == 1:
                stuck_level += 1
            else:
                stuck_level = max(0, stuck_level - 1)
            sentences = self.extract_scrambled_sentences()
            if not sentences:
                return False
            any_moved = False
            for idx, sent in enumerate(sentences):
                target = self.build_target(sent['words'], sent['statuses'], stuck_level)
                if sent['words'] != target:
                    if self.apply_target(sent['element'], target):
                        any_moved = True
                        time.sleep(0.12)
            if not any_moved and stuck_level > 5:
                self.click_repeat(settle=FAST_ACTION_SETTLE_SECONDS)
                time.sleep(0.3)
                stuck_level = 0
            self.click_correction()
            time.sleep(0.4)
        return self.get_result_score() >= 51

    def extract_scrambled_sentences(self):
        sentences = []
        try:
            for item in self.page.locator(".exercise-items .item").all():
                scrambled = item.locator(".scrambled-sentence").first
                if scrambled.count() == 0:
                    continue
                words = []
                statuses = []
                for block in scrambled.locator(".scrambled-block").all():
                    text = block.inner_text().strip()
                    classes = block.get_attribute("class") or ""
                    if text:
                        words.append(text)
                        if "success" in classes:
                            statuses.append("success")
                        elif "error" in classes:
                            statuses.append("error")
                        else:
                            statuses.append("unknown")
                sentences.append({"element": scrambled, "words": words, "statuses": statuses})
        except Exception:
            pass
        return sentences

    def build_target(self, words, statuses, stuck_level=0):
        modes = ["prep_first", "object_first", "random"]
        mode = modes[min(stuck_level, len(modes) - 1)] if stuck_level > 0 else "prep_first"
        heuristic = self.arrange_words(words, mode)
        success_positions = {}
        error_indices = []
        error_words = []
        for i, (word, status) in enumerate(zip(words, statuses)):
            if status == "success":
                success_positions[i] = word
            else:
                error_indices.append(i)
                error_words.append(word)
        heuristic_filtered = []
        for w in heuristic:
            if w in error_words and w not in heuristic_filtered:
                heuristic_filtered.append(w)
        for w in error_words:
            if w not in heuristic_filtered:
                heuristic_filtered.append(w)
        if mode == "random" and len(heuristic_filtered) > 1:
            random.shuffle(heuristic_filtered)
        target = list(words)
        for pos, word in success_positions.items():
            target[pos] = word
        for i, pos in enumerate(error_indices):
            if i < len(heuristic_filtered):
                target[pos] = heuristic_filtered[i]

        if not success_positions:
            target = self._third_conditional_target(words)
        return target

    def _third_conditional_target(self, words):
        if "would" not in [w.lower() for w in words] or "have" not in [w.lower() for w in words]:
            return list(words)

        punctuation = [w for w in words if w in {".", "!", "?", ","}]
        body = [w for w in words if w not in punctuation]
        lower = [w.lower() for w in body]

        word_set = {w.lower() for w in body}
        if {"i", "would", "have", "spent", "my", "vacation", "in",
            "the caribbean"}.issubset(word_set):
            return ["I", "would", "have", "spent", "my", "vacation",
                    "in", "the Caribbean"] + punctuation
        if {"you", "would", "not", "have", "received", "a", "ticket"}.issubset(word_set):
            return ["you", "would", "not", "have", "received", "a",
                    "ticket"] + punctuation

        subjects = {"i", "you", "he", "she", "we", "they", "it"}
        subject_i = next((i for i, w in enumerate(lower) if w in subjects), None)
        if subject_i is None:
            return list(words)

        subject = body[subject_i]
        rest = body[:subject_i] + body[subject_i + 1:]
        rest_lower = [w.lower() for w in rest]
        aux = []
       
        for aux_word in ("would", "not", "have"):
            try:
                pos = rest_lower.index(aux_word)
            except ValueError:
                continue
            aux.append(rest.pop(pos))
            rest_lower.pop(pos)

        verb_words = {"spent", "received", "seen", "gone", "done", "taken",
                      "made", "lost", "won", "forgotten", "broken", "driven"}
        verb_pos = next((i for i, w in enumerate(rest_lower) if w in verb_words), None)
        if verb_pos is None:
            return list(words)
        verb = rest.pop(verb_pos)

        prep = {"in", "on", "at", "to", "for", "from", "with", "by"}
        prep_pos = next((i for i, w in enumerate(rest) if w.lower() in prep), None)
        trailing = []
        if prep_pos is not None:
            trailing = rest[prep_pos:]
            rest = rest[:prep_pos]

        determiners = {"a", "an", "the", "my", "your", "his", "her", "our", "their"}
        ordered = []
        for w in rest:
            if w.lower() in determiners:
                ordered.insert(0, w)
            else:
                ordered.append(w)
        ordered.extend(trailing)
        return [subject] + aux + [verb] + ordered + punctuation

    def arrange_words(self, words, mode="prep_first"):
        subjects = {"i", "i'm", "i am", "he", "he's", "he is", "she", "she's", "she is",
                    "we", "we're", "we are", "they", "they're", "they are",
                    "you", "you're", "you are", "it", "it's", "it is"}
        aux_verbs = {"am", "is", "are", "was", "were", "'s", "'re", "'m",
                     "will", "would", "can", "could", "should", "may", "might", "must",
                     "have", "has", "had"}
        cats = {"subject": [], "aux": [], "main_verb": [], "prep_phrase": [],
                "object": [], "time": [], "punct": [], "unknown": []}
        for word in words:
            w = word.lower().strip()
            if w in [".", "?", "!", ","]:
                cats["punct"].append(word)
            elif w in subjects:
                cats["subject"].append(word)
            elif w in aux_verbs or w.startswith("'"):
                cats["aux"].append(word)
            else:
                cats["unknown"].append(word)
        result = []
        result += cats["subject"]
        result += cats["aux"]
        result += cats["unknown"]
        result += cats["punct"]
        return result

class ScrambledTableSolver(BaseSolver):
    MAX_ITERATIONS = 8

    def solve(self):
        self.logger.info("=== Scrambled Table Solver ===")
        self.start_timer()
        if not self.wait_for_page_ready():
            return False

        question_text = self._extract_question_text()
        cached = self.answer_db.get(question_text)
        cached_map = cached.get("answers") if isinstance(cached, dict) else None
        if isinstance(cached, dict) and cached.get("type") == "scrambled-table" and cached_map:
            self.step(f"[CACHE] Using saved table answers ({len(cached_map)} items)")
            if self._arrange_cells_js(cached_map):
                self.step("[CACHE] JS arrange completed")
                self.click_correction(settle=0.2)
                if self.get_result_score() >= self.PASS_LINE:
                    self.logger.info(f"[CACHE] Scrambled table completed in {self.stop_timer()}")
                    return True

        self.click_correction()
        time.sleep(0.2)
        if not self.click_solution(settle=0.4):
            return False
        time.sleep(0.3)

        correct_map = self.extract_correct_mapping()
        if not correct_map or all(m.get("text") is None for m in correct_map):
            self.logger.warning("Failed to extract scrambled table mapping")
            return False
        self.step(f"Extracted {len(correct_map)} items")

        if self.answer_db is not None:
            question_text = self._extract_question_text()
            self.answer_db[question_text] = {
                "type": "scrambled-table",
                "answers": correct_map,
                "timestamp": datetime.now().isoformat()
            }
            self.step(f"Saved scrambled table to DB")

        self.click_repeat(settle=0.3)
        time.sleep(0.2)

        if self._arrange_cells_js(correct_map):
            self.step("JS arrange completed (fast path)")
            self.click_correction(settle=0.2)
            time.sleep(0.2)
            if self.get_result_score() >= self.PASS_LINE:
                return True

        for iteration in range(self.MAX_ITERATIONS):
            score = self.get_result_score()
            self.logger.info(f"--- Iteration {iteration+1}/{self.MAX_ITERATIONS} | Score: {score} ---")
            if score >= 51:
                elapsed = self.stop_timer()
                self.logger.info(f"SUCCESS! Time: {elapsed}")
                return True
            self.arrange_cells(correct_map, fast=(iteration == 0))
            self.click_correction(settle=0.2)
            time.sleep(0.2)
        return self.get_result_score() >= 51

    def _arrange_cells_js(self, correct_map):
        try:
            result = self.page.evaluate("""
                (correctMap) => {
                    const items = document.querySelectorAll('.exercise-items .item');
                    let success = true;
                    
                    correctMap.forEach(entry => {
                        const idx = entry.idx;
                        const targetText = entry.text;
                        if (!targetText || idx >= items.length) return;
                        
                        const targetItem = items[idx];
                        const targetSlot = targetItem.querySelector('.scrambled-cell-container');
                        if (!targetSlot) { success = false; return; }
                        
                        // Find source cell with matching text
                        const allCells = document.querySelectorAll('.scrambled-cell');
                        let source = null;
                        for (const cell of allCells) {
                            if ((cell.innerText || '').trim() === targetText) {
                                source = cell;
                                break;
                            }
                        }
                        if (!source) { success = false; return; }
                        
                        // Check if already correct
                        const currentCell = targetSlot.querySelector('.scrambled-cell');
                        if (currentCell && (currentCell.innerText || '').trim() === targetText) {
                            return; // Already correct
                        }
                        
                        // Move source to target slot (DOM manipulation)
                        targetSlot.appendChild(source);
                        
                        // Trigger drag/drop events for frameworks that need them
                        const dragStart = new DragEvent('dragstart', {bubbles: true, dataTransfer: new DataTransfer()});
                        source.dispatchEvent(dragStart);
                        
                        const dragOver = new DragEvent('dragover', {bubbles: true});
                        targetSlot.dispatchEvent(dragOver);
                        
                        const drop = new DragEvent('drop', {bubbles: true});
                        targetSlot.dispatchEvent(drop);
                        
                        const dragEnd = new DragEvent('dragend', {bubbles: true});
                        source.dispatchEvent(dragEnd);
                    });
                    
                    // Trigger change events on all containers
                    const containers = document.querySelectorAll('.exercise-items .item .scrambled-cell-container');
                    containers.forEach(c => {
                        c.dispatchEvent(new Event('change', {bubbles: true}));
                        c.dispatchEvent(new Event('drop', {bubbles: true}));
                    });
                    
                    return success;
                }
            """, correct_map)
            return result
        except Exception as e:
            self.logger.debug(f"[SCRAMBLED-TABLE] JS arrange failed: {e}")
            return False

    def extract_correct_mapping(self):
        try:
            result = self.page.evaluate("""
                () => {
                    const items = document.querySelectorAll('.exercise-items .item');
                    const mapping = [];
                    items.forEach((item, idx) => {
                        const cell = item.querySelector('.scrambled-cell');
                        if (!cell) {
                            mapping.push({idx: idx, text: null});
                            return;
                        }
                        const text = (cell.innerText || '').trim();
                        mapping.push({idx: idx, text: text});
                    });
                    return mapping;
                }
            """)
            return result if result else []
        except Exception as e:
            self.logger.error(f"extract_correct_mapping error: {e}")
            return []

    def arrange_cells(self, correct_map, fast=False):
        moved = False
        try:
            items = self.page.locator(".exercise-items .item").all()
            for entry in correct_map:
                idx = entry["idx"]
                target_text = entry["text"]
                if not target_text or idx >= len(items):
                    continue
                target_item = items[idx]
                target_slot = target_item.locator(".scrambled-cell-container").first
                if target_slot.count() == 0:
                    continue
                current = target_slot.locator(".scrambled-cell").first
                if current.count() > 0:
                    try:
                        if current.inner_text().strip() == target_text:
                            continue
                    except Exception:
                        pass
                all_cells = self.page.locator(".scrambled-cell").all()
                source = None
                for cell in all_cells:
                    try:
                        if cell.inner_text().strip() == target_text:
                            source = cell
                            break
                    except Exception:
                        continue
                if source is None:
                    self.logger.warning(f"Cell '{target_text}' not found in DOM")
                    continue
                self._scroll_into_view(source)
                self._scroll_into_view(target_slot)
                placed = self.fast_drag(source, target_slot) if fast else False
                if not placed:
                    placed = self.precise_drag(source, target_slot)
                if placed:
                    moved = True
                    if not fast:
                        time.sleep(0.05)
        except Exception as e:
            self.logger.error(f"arrange_cells error: {e}")
        return moved

class SingleChoiceSolver(BaseSolver):
    MAX_ITERATIONS = 8

    def _apply_cached_answers(self, answers, subtype):
        try:
            if subtype == "legacy":
                options = self.page.locator(".choice-option").all()
                if answers and answers[0] < len(options):
                    inp = options[answers[0]].locator("input.choice").first
                    if inp.count() > 0:
                        return self._click_choice_option(inp, "cached-legacy")
                return False

            items = self.page.locator(".exercise-items .item.choice-item").all()
            if not items:
                return False

            if subtype == "group":
                idx = answers[0] if answers else 0
                opts = items[0].locator("input.choice").all()
                if idx < len(opts):
                    return self._click_choice_option(opts[idx], "cached-group")
                return False

            for i, idx in enumerate(answers):
                if i >= len(items):
                    break
                opts = items[i].locator("input.choice").all()
                if idx < len(opts):
                    self._click_choice_option(opts[idx], f"cached-{i}")
                    time.sleep(0.1)
            return True
        except Exception as e:
            self.logger.error(f"[CACHE] apply failed: {e}")
            return False

    def _save_choice_to_db(self, answers, subtype=None):
        if self.answer_db is None:
            return
        try:
            question_text = self._extract_question_text()
            entry = {
                "type": "single-choice",
                "answers": list(answers),      
                "timestamp": datetime.now().isoformat(),
            }
            if subtype:
                entry["subtype"] = subtype
            self.answer_db[question_text] = entry
            self.step(f"[DB] Saved single-choice ({subtype or 'multi'}): {answers}")
        except Exception as e:
            self.logger.error(f"[DB] Save single-choice failed: {e}")

    def solve(self):
       
        cached = self._find_cached_answer()
        if cached and cached.get("type") == "single-choice":
            subtype = cached.get("subtype", "multi")
            answers = cached.get("answers", [])
            self.step(f"[CACHE] Found saved answers ({subtype}): {answers}")
            if self._apply_cached_answers(answers, subtype):
                self.click_correction()
                time.sleep(1)
                if self.get_result_score() >= self.PASS_LINE:
                    self.step("[CACHE] Passed with cached answers!")
                    return True
                self.step("[CACHE] Cached answers failed — falling back to solve")
                self.click_repeat()
                time.sleep(1)

        self.logger.info("=== Single Choice Solver ===")
        self.start_timer()

        if not self.wait_for_page_ready():
            return False

        if self.already_passed():
            self.logger.info("[CHOICE] Already passed (score >= 51)")
            return True

        self.click_correction()
        time.sleep(1)

        has_solution = self.click_solution()
        time.sleep(1.5)

        try:
            item_count = self.page.locator(".exercise-items .item.choice-item").count()
        except Exception:
            item_count = 0

        if item_count == 0:
            return self._solve_single_legacy()

        try:
            group_count = self.page.evaluate("""
                () => new Set(Array.from(document.querySelectorAll(
                    '.exercise-items .item.choice-item input.choice'
                )).map(input => input.name || input.getAttribute('data-group-id') || input.value)).size
            """)
        except Exception:
            group_count = item_count

        if item_count > 1 and group_count == 1:
            return self._solve_single_group_choice()

        if not has_solution:
            self.logger.info("[CHOICE] No solution button — using fallback")
            return self._solve_without_solution(item_count)

        correct_per_item = self._find_correct_per_item_js()
        if correct_per_item:
           
            normalised = []
            for c in correct_per_item:
                if isinstance(c, list):
                    normalised.append(c[0] if c else 0)
                elif isinstance(c, int):
                    normalised.append(c)
                else:
                    normalised.append(0)
            self._save_choice_to_db(normalised, subtype="multi")

        return self._solve_multi_item(item_count)

    def _solve_without_solution(self, item_count):
        items = self.page.locator(".exercise-items .item.choice-item").all()
        if not items:
            return False

        opts_counts = []
        for item in items:
            n = item.locator("input.choice").count()
            if n == 0:
                return False
            opts_counts.append(n)

        self.click_repeat()
        time.sleep(1.2)

        current = [0] * item_count
        items = self.page.locator(".exercise-items .item.choice-item").all()
        for i in range(min(item_count, len(items))):
            try:
                opts = items[i].locator("input.choice").all()
                if opts:
                    self._click_choice_option(opts[0], f"brute-init-{i}")
                    time.sleep(0.1)
            except Exception:
                pass

        self.click_correction()
        time.sleep(1.5)
        score = self.get_result_score()
        self.step(f"[BRUTE] initial score={score}")

        if score >= self.PASS_LINE:
           
            self._save_choice_to_db(current, subtype="brute")
            return True

        max_iter = item_count * 3
        iteration = 0
        improved = True

        while improved and iteration < max_iter and score < self.PASS_LINE:
            improved = False
            iteration += 1

            items = self.page.locator(".exercise-items .item.choice-item").all()
            if len(items) < item_count:
                if score >= self.PASS_LINE:
                    self._save_choice_to_db(current, subtype="brute")
                return score >= self.PASS_LINE

            for i in range(item_count):
                n_opts = opts_counts[i]
                if n_opts <= 1:
                    continue

                new_idx = (current[i] + 1) % n_opts

                opts = items[i].locator("input.choice").all()
                if new_idx >= len(opts):
                    continue
                if not self._click_choice_option(opts[new_idx], f"brute-{i}-{new_idx}"):
                    continue
                time.sleep(0.2)

                self.click_correction()
                time.sleep(1.5)
                new_score = self.get_result_score()

                if new_score > score:
                    score = new_score
                    current[i] = new_idx
                    improved = True
                    self.step(f"[BRUTE] item {i} → option {new_idx}, score={score}")
                    if score >= self.PASS_LINE:
                       
                        self._save_choice_to_db(current, subtype="brute")
                        return True
                else:
                    items = self.page.locator(
                        ".exercise-items .item.choice-item"
                    ).all()
                    if i < len(items):
                        opts = items[i].locator("input.choice").all()
                        if current[i] < len(opts):
                            self._click_choice_option(
                                opts[current[i]], f"brute-restore-{i}")
                            time.sleep(0.2)

        if score >= self.PASS_LINE:
            self._save_choice_to_db(current, subtype="brute")

        return score >= self.PASS_LINE

    def _solve_multi_item(self, item_count):
        correct_per_item = None
        for wait in range(3):
            correct_per_item = self._find_correct_per_item_js()
            if correct_per_item and all(c is not None for c in correct_per_item):
                break
            time.sleep(0.3)
        if not correct_per_item or any(c is None for c in correct_per_item):
            correct_per_item = self._find_correct_per_item()
            if not correct_per_item or any(c is None for c in correct_per_item):
                return False

        normalised = []
        for choices in correct_per_item:
            if isinstance(choices, list):
                if len(choices) != 1 or not isinstance(choices[0], int):
                    if len(choices) >= 1 and isinstance(choices[0], int):
                        choices = choices[0]
                    else:
                        return False
                else:
                    choices = choices[0]
            if not isinstance(choices, int):
                return False
            normalised.append(choices)
        correct_per_item = normalised
        self.step(f"Detected correct per item: {correct_per_item}")

        if self.get_result_score() >= self.PASS_LINE:
            return True

        self.click_repeat()
        time.sleep(1)

        for iteration in range(self.MAX_ITERATIONS):
            score = self.get_result_score()
            self.logger.info(
                f"--- Iteration {iteration+1}/{self.MAX_ITERATIONS} | Score: {score} ---")
            if score >= self.PASS_LINE:
                return True
            items = self.page.locator(".exercise-items .item.choice-item").all()
            for item_idx, correct_idx in enumerate(correct_per_item):
                if correct_idx is None or item_idx >= len(items):
                    continue
                item = items[item_idx]
                opts = item.locator("input.choice").all()
                if correct_idx < len(opts):
                    self._click_choice_option(
                        opts[correct_idx], f"multi-{item_idx}")
                    time.sleep(0.15)
            self.click_correction()
            time.sleep(1)
        return self.get_result_score() >= self.PASS_LINE

    def _solve_single_group_choice(self):
        options = self.page.locator(
            '.exercise-items .item.choice-item input.choice').all()
        correct_index = None
        for index, option in enumerate(options):
            try:
                if option.is_checked(timeout=1000):
                    correct_index = index
                    break
            except Exception:
                continue
        if correct_index is None:
            self.logger.warning('[CHOICE] solution did not expose a selected option')
            return False
        self.step(f'Detected grouped correct option: {correct_index}')

        self._save_choice_to_db([correct_index], subtype="group")

        if self.get_result_score() >= self.PASS_LINE:
            return True
        self.click_repeat()
        time.sleep(1)
        for iteration in range(self.MAX_ITERATIONS):
            score = self.get_result_score()
            self.logger.info(
                f'--- Grouped iteration {iteration+1}/{self.MAX_ITERATIONS} | Score: {score} ---')
            if score >= self.PASS_LINE:
                return True
            current = self.page.locator(
                '.exercise-items .item.choice-item input.choice').all()
            if correct_index >= len(current):
                return False
            if not self._click_choice_option(
                    current[correct_index], "group"):
                return False
            self.click_correction()
            time.sleep(1)
        return self.get_result_score() >= self.PASS_LINE

    def _find_correct_per_item_js(self):
        try:
            result = self.page.evaluate("""
                () => {
                    const items = document.querySelectorAll('.exercise-items .item.choice-item');
                    const results = [];

                    const isGreenish = (str) => {
                        if (!str) return false;
                        const s = str.toString().toLowerCase();
                        return s.includes('success') || s.includes('correct') ||
                               s.includes('result-green') || s.includes('bg-success') ||
                               s.includes('green') || s.includes('is-correct') ||
                               s.includes('is-checked') || s.includes('selected-correct');
                    };

                    const isSolutionSelected = (el) => {
                        if (!el) return false;
                        const cls = (el.className || '').toString().toLowerCase();
                        const aria = (el.getAttribute('aria-checked') || '').toLowerCase();
                        const data = (el.getAttribute('data-correct') || '').toLowerCase();
                        return aria === 'true' || data === 'true' ||
                               /(^|[-_ ])(selected|active|checked|solution)([-_ ]|$)/.test(cls);
                    };

                    const isGreenStyle = (el) => {
                        try {
                            const style = window.getComputedStyle(el);
                            const values = [style.backgroundColor, style.color,
                                style.borderColor, style.outlineColor].join(' ');
                            const colors = [...values.matchAll(
                                /rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/g
                            )];
                            return colors.some(m => {
                                const r = parseInt(m[1]), g = parseInt(m[2]), b = parseInt(m[3]);
                                return g > r + 25 && g > b + 15;
                            });
                        } catch(e) { return false; }
                    };

                    items.forEach(item => {
                        const corrects = [];
                        const opts = item.querySelectorAll('input.choice');

                        for (let i = 0; i < opts.length; i++) {
                            if (opts[i].checked || isSolutionSelected(opts[i]) ||
                                isSolutionSelected(opts[i].closest('label')) ||
                                isSolutionSelected(opts[i].parentElement)) {
                                if (!corrects.includes(i)) corrects.push(i);
                            }
                        }

                        if (corrects.length === 0) {
                            for (let i = 0; i < opts.length; i++) {
                                if (isGreenish(opts[i].className)) {
                                    if (!corrects.includes(i)) corrects.push(i);
                                }
                            }
                        }

                        if (corrects.length === 0) {
                            const labels = item.querySelectorAll('.choice-option');
                            for (let i = 0; i < labels.length; i++) {
                                const all = [labels[i], ...labels[i].querySelectorAll('*')];
                                for (const el of all) {
                                    if (isGreenish(el.className) || isSolutionSelected(el) ||
                                        isGreenStyle(el)) {
                                        if (!corrects.includes(i)) corrects.push(i);
                                        break;
                                    }
                                }
                            }
                        }

                        if (corrects.length === 0) {
                            const labels = item.querySelectorAll('.choice-option');
                            for (let i = 0; i < labels.length; i++) {
                                try {
                                    const style = window.getComputedStyle(labels[i]);
                                    const bg = style.backgroundColor || '';
                                    const m = bg.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
                                    if (m) {
                                        const r = parseInt(m[1]);
                                        const g = parseInt(m[2]);
                                        const b = parseInt(m[3]);
                                    if (g > r + 30 && g > b + 30) {
                                        if (!corrects.includes(i)) corrects.push(i);
                                    }
                                    }
                                } catch(e) {}
                            }
                        }

                        results.push(corrects);
                    });
                    return results;
                }
            """)
            return result if result else None
        except Exception as e:
            self.logger.error(f"_find_correct_per_item_js error: {e}")
            return None

    def _find_correct_per_item(self):
        results = []
        try:
            items = self.page.locator(".exercise-items .item.choice-item").all()
            for item in items:
                results.append(self._find_correct_in_item(item))
        except Exception:
            pass
        return results

    def _find_correct_in_item(self, item):
        try:
            opts = item.locator("input.choice").all()
            for i, opt in enumerate(opts):
                try:
                    if opt.is_checked(timeout=1000):
                        return i
                except Exception:
                    pass
            labels = item.locator(".choice-option").all()
            for i, label in enumerate(labels):
                try:
                    all_el = [label] + label.locator("*").all()
                    for el in all_el:
                        cls = (el.get_attribute("class") or "").lower()
                        aria = (el.get_attribute("aria-checked") or "").lower()
                        data = (el.get_attribute("data-correct") or "").lower()
                        if (any(k in cls for k in ("success", "correct", "result-green", "bg-success"))
                                or aria == "true" or data == "true"
                                or any(k in cls.split() for k in ("selected", "active", "checked", "solution"))):
                            return i
                except Exception:
                    pass
        except Exception:
            pass
        return None

    def _solve_single_legacy(self):
        correct_idx = None
        for wait in range(3):
            correct_idx = self.find_correct_option()
            if correct_idx is not None:
                break
            time.sleep(0.3)
        if correct_idx is None:
            return False

        self._save_choice_to_db([correct_idx], subtype="legacy")

        if self.get_result_score() >= self.PASS_LINE:
            return True
        self.click_repeat()
        time.sleep(1)
        for iteration in range(self.MAX_ITERATIONS):
            score = self.get_result_score()
            if score >= self.PASS_LINE:
                return True
            options = self.page.locator(".choice-option").all()
            if correct_idx < len(options):
                inp = options[correct_idx].locator("input.choice").first
                if inp.count() > 0:
                    self._click_choice_option(inp, "legacy")
                    time.sleep(0.5)
            self.click_correction()
            time.sleep(1)
        return self.get_result_score() >= self.PASS_LINE

    def find_correct_option(self):
        try:
            idx = self.page.evaluate("""
                () => {
                    const radios = document.querySelectorAll('input.choice');
                    for (let i = 0; i < radios.length; i++) {
                        if (radios[i].checked) return i;
                    }
                    const opts = document.querySelectorAll('.choice-option');
                    for (let i = 0; i < opts.length; i++) {
                        const all = [opts[i], ...opts[i].querySelectorAll('*')];
                        for (const el of all) {
                            const cls = (el.className || '').toString().toLowerCase();
                            if (cls.includes('success') || cls.includes('correct') ||
                                cls.includes('result-green') || cls.includes('bg-success')) {
                                return i;
                            }
                        }
                    }
                    return -1;
                }
            """)
            if idx is not None and idx >= 0:
                return idx
        except Exception:
            pass
        return None

class MultipleChoiceSolver(BaseSolver):
    MAX_ITERATIONS = 8

    def _save_multiple_choice_to_db(self, correct_per_item):
        if self.answer_db is None:
            return
        try:
            question_text = self._extract_question_text()
            self.answer_db[question_text] = {
                "type": "multiple-choice",
                "answers": [list(c) for c in correct_per_item],
                "timestamp": datetime.now().isoformat(),
            }
            self.step(f"[DB] Saved multiple-choice: {correct_per_item}")
        except Exception as e:
            self.logger.error(f"[DB] Save multiple-choice failed: {e}")

    def _apply_cached_multiple_answers(self, answers):
        try:
            items = self.page.locator(".exercise-items .item.choice-item").all()
            if not items:
                return False
            for item_idx, correct_indices in enumerate(answers):
                if item_idx >= len(items):
                    continue
                item = items[item_idx]
                opts = item.locator("input.choice").all()
                for correct_idx in correct_indices:
                    if correct_idx < len(opts):
                        self._click_choice_option(
                            opts[correct_idx],
                            f"multi-cache-{item_idx}")
                        time.sleep(0.1)
            return True
        except Exception as e:
            self.logger.error(f"[MULTI] cache apply failed: {e}")
            return False

    def solve(self):
        self.logger.info("=== Multiple Choice Solver ===")
        self.start_timer()
        if not self.wait_for_page_ready():
            return False

        cached = self._find_cached_answer()
        if cached and cached.get("type") == "multiple-choice":
            answers = cached.get("answers", [])
            self.step(f"[CACHE] Found saved multiple-choice answers: {answers}")
            if self._apply_cached_multiple_answers(answers):
                self.click_correction()
                time.sleep(1)
                if self.get_result_score() >= self.PASS_LINE:
                    self.step("[CACHE] Passed with cached multiple-choice answers!")
                    return True
                self.step("[CACHE] Cached multiple-choice answers failed — falling back to solve")
                self.click_repeat()
                time.sleep(1)

        self.click_correction()
        time.sleep(1)

        has_solution = self.click_solution()
        time.sleep(2.5)
        try:
            item_count = self.page.locator(".exercise-items .item.choice-item").count()
        except Exception:
            item_count = 0
        if item_count == 0:
            return False

        correct_per_item = None
        for wait in range(3):
            correct_per_item = self._find_correct_per_item_js()
            if correct_per_item and any(len(c) > 0 for c in correct_per_item):
                break
            time.sleep(0.3)

        if not correct_per_item or all(len(c) == 0 for c in correct_per_item):
            if has_solution:
                return False
            self.logger.warning("[MULTI] No revealed solution; falling back to greedy selection")
            return self._solve_without_solution()

        self._save_multiple_choice_to_db(correct_per_item)

        if self.get_result_score() >= 51:
            return True
        self.click_repeat()
        time.sleep(1)
        for iteration in range(self.MAX_ITERATIONS):
            score = self.get_result_score()
            if score >= 51:
                return True
            items = self.page.locator(".exercise-items .item.choice-item").all()
            for item_idx, correct_indices in enumerate(correct_per_item):
                if not correct_indices or item_idx >= len(items):
                    continue
                item = items[item_idx]
                opts = item.locator("input.choice").all()
                for correct_idx in correct_indices:
                    if correct_idx < len(opts):
                        self._click_choice_option(
                            opts[correct_idx],
                            f"multi-solve-{item_idx}")
                        time.sleep(0.1)
            self.click_correction()
            time.sleep(1)
        return self.get_result_score() >= 51

    def _solve_without_solution(self):
        try:
            items = self.page.locator(".exercise-items .item.choice-item").all()
            if not items:
                return False

            self.click_repeat()
            time.sleep(1)
            current_score = self.get_result_score()

            for _ in range(12):
                improved = False
                for item_idx, item in enumerate(items):
                    opts = item.locator("input.choice").all()
                    if not opts:
                        continue
                    for opt_idx in range(len(opts)):
                        was_selected = False
                        try:
                            was_selected = opts[opt_idx].is_checked()
                        except Exception:
                            pass

                        try:
                            self._click_choice_option(
                                opts[opt_idx],
                                f"multi-brute-{item_idx}-{opt_idx}")
                            self.click_correction()
                            time.sleep(0.7)
                            new_score = self.get_result_score()
                            if new_score > current_score:
                                current_score = new_score
                                improved = True
                                continue
                            self._click_choice_option(
                                opts[opt_idx],
                                f"multi-brute-restore-{item_idx}-{opt_idx}")
                            self.click_correction()
                            time.sleep(0.7)
                        except Exception:
                            pass
                if not improved:
                    break

            return self.get_result_score() >= 51
        except Exception as e:
            self.logger.error(f"[MULTI] fallback failed: {e}")
            return False

    def _find_correct_per_item_js(self):
        try:
            result = self.page.evaluate("""
                () => {
                    const items = document.querySelectorAll('.exercise-items .item.choice-item');
                    const results = [];
                    items.forEach(item => {
                        const corrects = [];
                        const opts = item.querySelectorAll('input.choice');
                        for (let i = 0; i < opts.length; i++) {
                            if (opts[i].checked) corrects.push(i);
                        }
                        if (corrects.length === 0) {
                            const labels = item.querySelectorAll('.choice-option');
                            for (let i = 0; i < labels.length; i++) {
                                const all = [labels[i], ...labels[i].querySelectorAll('*')];
                                for (const el of all) {
                                    const cls = (el.className || '').toString().toLowerCase();
                                    if (cls.includes('success') || cls.includes('correct') ||
                                        cls.includes('result-green') || cls.includes('bg-success')) {
                                        corrects.push(i);
                                        break;
                                    }
                                }
                            }
                        }
                        results.push(corrects);
                    });
                    return results;
                }
            """)
            return result if result else None
        except Exception as e:
            self.logger.error(f"_find_correct_per_item_js error: {e}")
            return None

class PictureChoiceSolver(BaseSolver):
    MAX_ITERATIONS = 10

    def _detect_reorder_mode(self):
        try:
            source_imgs = self.page.locator(".draggable-container img").all()
            visible_sources = [img for img in source_imgs if img.is_visible(timeout=200)]
            
            slot_imgs = self.page.locator(".exercise-items .drag-drop-placeholder img, .exercise-items .item img.picture-gap, .exercise-items .item img.drag-drop").all()
            
            self.logger.info(f"[PICTURE] Detection: {len(visible_sources)} visible source images, {len(slot_imgs)} images in slots")
            return len(visible_sources) == 0 and len(slot_imgs) > 0
        except Exception as e:
            self.logger.debug(f"[PICTURE] Reorder detection failed: {e}")
            return False

    def _reorder_slots_by_mapping(self, mapping):
        try:
            items = self.page.locator(".exercise-items .item").all()
            if not items:
                return 0
            
            moved = 0
            current_images = []
            for idx, item in enumerate(items):
                img = item.locator("img.picture-gap, img.drag-drop, .drag-drop-placeholder img").first
                if img.count() > 0:
                    src = img.get_attribute("src") or ""
                    dd_id = img.get_attribute("data-drag-drop-id") or ""
                    current_images.append({"item_idx": idx, "img": img, "src": src, "dd_id": dd_id})
                else:
                    current_images.append({"item_idx": idx, "img": None, "src": "", "dd_id": ""})
            
            self.logger.info(f"[PICTURE] Current slot images: {[img['src'] for img in current_images]}")
            
            for entry in mapping:
                target_idx = entry["item_idx"]
                target_src = entry["src"]
                target_dd_id = entry["drag_id"]
                if not target_src or target_idx >= len(current_images):
                    continue
                
                source_info = None
                for info in current_images:
                    if info["src"] == target_src or (target_dd_id and info["dd_id"] == target_dd_id):
                        source_info = info
                        break
                
                if not source_info:
                    self.logger.warning(f"[PICTURE] Image with src '{target_src}' not found in any slot")
                    continue
                
                source_idx = source_info["item_idx"]
                if source_idx == target_idx:
                    self.logger.info(f"[PICTURE] Slot {target_idx} already has correct image")
                    continue
                
                self.logger.info(f"[PICTURE] Swapping slot {source_idx} -> {target_idx} (image: {target_src})")
                
                source_item = items[source_idx]
                target_item = items[target_idx]
                
                source_img = source_item.locator("img.picture-gap, img.drag-drop, .drag-drop-placeholder img").first
                target_placeholder = target_item.locator(".drag-drop-placeholder").first
                
                if source_img.count() == 0:
                    continue
                
                try:
                    if target_placeholder.count() > 0:
                        self._safe_click(
                            target_placeholder, timeout=2000,
                            description="picture-target")
                    else:
                        self._safe_click(
                            target_item, timeout=2000,
                            description="picture-target-item")
                    time.sleep(0.1)
                    self._safe_click(
                        source_img, timeout=2000, description="picture-source")
                    time.sleep(0.3)
                    
                    items_now = self.page.locator(".exercise-items .item").all()
                    if target_idx < len(items_now):
                        placed_img = items_now[target_idx].locator("img").first
                        placed_src = placed_img.get_attribute("src") if placed_img.count() > 0 else ""
                        if placed_src == target_src:
                            moved += 1
                            self.logger.info(f"[PICTURE] Swap successful for slot {target_idx}")
                        else:
                            self.logger.warning(f"[PICTURE] Swap may have failed for slot {target_idx}, got: {placed_src}")
                except Exception as e:
                    self.logger.debug(f"[PICTURE] Click-swap failed for slot {target_idx}: {e}")
            
            return moved
        except Exception as e:
            self.logger.error(f"[PICTURE] Reorder slots error: {e}")
            return 0

    def solve(self):
        self.logger.info("=== Picture Choice Solver ===")
        self.start_timer()
        if not self.wait_for_page_ready():
            return False
        self.click_correction()
        time.sleep(0.3)
        if not self.click_solution():
            return False
        time.sleep(0.5)
        mapping = self.extract_solution_mapping()
        if not mapping or all(not m.get("src") for m in mapping):
            return False

        if self.answer_db is not None:
            question_text = self._extract_question_text()
            self.answer_db[question_text] = {
                "type": "picture-choice",
                "answers": mapping,
                "timestamp": datetime.now().isoformat()
            }
            self.step(f"Saved picture choice mapping to DB")

        self.click_repeat()
        time.sleep(0.3)

        for iteration in range(self.MAX_ITERATIONS):
            score = self.get_result_score()
            if score >= 51:
                return True
            if iteration > 0 and not self._restore_tiles_for_retry():
                self.logger.warning(
                    "[PICTURE] Cannot reset images for retry — stop trying")
                break

            is_reorder_mode = self._detect_reorder_mode()
            self.logger.info(
                "[PICTURE] Exercise mode: %s",
                "reorder-within-slots" if is_reorder_mode else "drag-from-source")

            if is_reorder_mode:
                self._reorder_slots_by_mapping(mapping)
            else:
                self.perform_picture_drag(mapping)

            time.sleep(0.2)
            self.click_correction()
            time.sleep(0.3)
        return self.get_result_score() >= 51

    def extract_solution_mapping(self):
        mapping = []
        try:
            items = self.page.locator(".exercise-items .item").all()
            for idx, item in enumerate(items):
                img = item.locator("img.picture-gap, img.drag-drop").first
                if img.count() == 0:
                    img = item.locator("img").first
                if img.count() > 0:
                    src = img.get_attribute("src") or ""
                    dd_id = img.get_attribute("data-drag-drop-id") or ""
                    mapping.append({"item_idx": idx, "src": src, "drag_id": dd_id})
                else:
                    mapping.append({"item_idx": idx, "src": "", "drag_id": ""})
        except Exception:
            pass
        return mapping

    GAP_IMG_SELECTOR = "img.picture-gap, img.drag-drop, .drag-drop-placeholder img"

    def _picture_slot_has(self, idx, target_src):
        try:
            items = self.page.locator(".exercise-items .item").all()
            if idx >= len(items):
                return False
            img = items[idx].locator(self.GAP_IMG_SELECTOR).first
            if img.count() == 0:
                return False
            return (img.get_attribute("src") or "") == target_src
        except Exception:
            return False

    def perform_picture_drag(self, mapping):
        moved = False
        destination_armed = False
        click_placed = 0
        drag_placed = 0
        try:
            for entry in mapping:
                idx = entry["item_idx"]
                target_src = entry["src"]
                target_id = entry["drag_id"]
                items = self.page.locator(".exercise-items .item").all()
                if idx >= len(items) or not target_src:
                    continue
                item = items[idx]
                if self._picture_slot_has(idx, target_src):
                    continue
                source = None
                if target_id:
                    source = self.page.locator(
                        f".draggable-container img[data-drag-drop-id='{target_id}']"
                    ).first
                    if source.count() == 0:
                        source = self.page.locator(f"img[data-drag-drop-id='{target_id}']").first
                    if source.count() == 0:
                        source = None
                if source is None and target_src:
                    source = self.page.locator(
                        ".draggable-container img[src=" + json.dumps(target_src) + "]"
                    ).first
                    if source.count() == 0:
                        source = self.page.locator(f"img[src='{target_src}']").first
                    if source.count() == 0:
                        source = None
                if source is None:
                    continue
                placeholder = item.locator(".drag-drop-placeholder").first
                target = placeholder if placeholder.count() > 0 else item
                placed = False
                used_drag = False
                try:
                    
                    if not destination_armed:
                        self._safe_click(
                            target, timeout=1500, description="picture-target")
                        destination_armed = True
                    self._safe_click(
                        source, timeout=1500, description="picture-source")
                    self.page.wait_for_timeout(100)

                    placed = self._picture_slot_has(idx, target_src)

                    if not placed:
                        items_now = self.page.locator(".exercise-items .item").all()
                        if idx < len(items_now):
                            target_now = items_now[idx].locator(".drag-drop-placeholder").first
                            if target_now.count() == 0:
                                target_now = items_now[idx]
                            source_now = self.page.locator(
                                ".draggable-container img[src=" + json.dumps(target_src) + "]"
                            ).first
                            if source_now.count() == 0:
                                source_now = self.page.locator(f"img[src='{target_src}']").first
                            if source_now.count() > 0:
                                self._safe_click(
                                    target_now, timeout=1000,
                                    description="picture-target-retry")
                                self._safe_click(
                                    source_now, timeout=1000,
                                    description="picture-source-retry")
                                self.page.wait_for_timeout(80)
                                placed = self._picture_slot_has(idx, target_src)
                except Exception as e:
                    self.logger.debug(f"[PICTURE] Click-to-place failed for item {idx}: {e}")

                if not placed:
                    
                    items_now = self.page.locator(".exercise-items .item").all()
                    source_now = self.page.locator(
                        ".draggable-container img[src=" + json.dumps(target_src) + "]"
                    ).first
                    if source_now.count() == 0:
                        source_now = self.page.locator(f"img[src='{target_src}']").first
                    if idx < len(items_now) and source_now.count() > 0:
                        target_now = items_now[idx].locator(".drag-drop-placeholder").first
                        target_now = target_now if target_now.count() > 0 else items_now[idx]
                        self.precise_drag(source_now, target_now)
                        self.page.wait_for_timeout(150)
                        placed = self._picture_slot_has(idx, target_src)
                        if placed:
                            drag_placed += 1
                            used_drag = True
                if placed:
                    moved = True
                    if not used_drag:
                        click_placed += 1
                    time.sleep(0.05)
            self.logger.info(
                "[PICTURE] Placement methods: %s by click, %s by drag fallback",
                click_placed, drag_placed)
        except Exception as e:
            self.logger.error(f"[PICTURE] Placement error: {e}")
        return moved

class ToggleSolutionSolver(BaseSolver):
    MAX_ITERATIONS = 10
    MAX_TOGGLE_CYCLES = 8

    def solve(self):
        self.logger.info("=== Toggle Solution Solver ===")
        self.start_timer()
        if not self.wait_for_page_ready():
            return False
        self.click_correction()
        time.sleep(0.3)
        if not self.click_solution():
            return False
        time.sleep(0.5)
        correct_answers = self.extract_correct_answers()
        if not correct_answers or all(a == "" for a in correct_answers):
            return False

        if self.answer_db is not None:
            question_text = self._extract_question_text()
            self.answer_db[question_text] = {
                "type": "toggle-solution",
                "answers": correct_answers,
                "timestamp": datetime.now().isoformat()
            }
            self.step(f"Saved toggle solution answers to DB")

        self.click_repeat()
        time.sleep(0.3)
        
        if self._set_all_gaps_js(correct_answers):
            self.step("Set all gaps via JS (fast path)")
            time.sleep(0.2)
            self.click_correction()
            time.sleep(0.3)
            if self.get_result_score() >= 51:
                return True

        for iteration in range(self.MAX_ITERATIONS):
            score = self.get_result_score()
            if score >= 51:
                return True
            self.set_all_gaps(correct_answers)
            time.sleep(0.3)
            self.click_correction()
            time.sleep(0.3)
        return self.get_result_score() >= 51

    def _norm(self, s):
        return (s or "").replace("\xa0", " ").strip()

    def extract_correct_answers(self):
        answers = []
        try:
            gaps = self.page.locator(".exercise-items .gap").all()
            for g in gaps:
                answers.append(self._norm(g.inner_text()))
        except Exception:
            pass
        return answers

    def _set_all_gaps_js(self, answers):
        try:
            result = self.page.evaluate("""
                (answers) => {
                    const gaps = document.querySelectorAll(".exercise-items .gap");
                    let success = true;
                    answers.forEach((ans, i) => {
                        if (i >= gaps.length || !ans) return;
                        const gap = gaps[i];
                        const container = gap.closest(".gap-container, [class*='gap-container']");
                        if (!container) { success = false; return; }
                        const toggle = container.querySelector("button");
                        if (!toggle) { success = false; return; }
                        
                        // Click toggle until it matches the answer
                        const norm = (s) => (s || "").replace(/\\u00a0/g, " ").trim();
                        let cur = norm(gap.innerText);
                        if (cur === ans) return;
                        
                        for (let cycle = 0; cycle < 10; cycle++) {
                            toggle.click();
                            cur = norm(gap.innerText);
                            if (cur === ans) break;
                        }
                        if (cur !== ans) success = false;
                    });
                    return success;
                }
            """, answers)
            return result
        except Exception as e:
            self.logger.debug(f"[TOGGLE] JS fast path failed: {e}")
            return False

    def set_all_gaps(self, answers):
        try:
            gaps = self.page.locator(".exercise-items .gap").all()
            for i, ans in enumerate(answers):
                if i >= len(gaps) or not ans:
                    continue
                gap = gaps[i]
                try:
                    gap.scroll_into_view_if_needed()
                except Exception:
                    pass
                container = gap.locator("xpath=ancestor::div[contains(@class,'gap-container')]").first
                if container.count() == 0:
                    continue
                toggle = container.locator("button").first
                if toggle.count() == 0:
                    continue
                cur = self._norm(gap.inner_text())
                if cur == ans:
                    continue
                for cycle in range(self.MAX_TOGGLE_CYCLES):
                    if not self._safe_click(
                            toggle, timeout=1000, description="toggle-gap"):
                        break
                    time.sleep(0.05)
                    cur = self._norm(gap.inner_text())
                    if cur == ans:
                        break
        except Exception:
            pass

class MarkTextSolver(BaseSolver):
    MAX_ITERATIONS = 10

    def solve(self):
        self.logger.info("=== Mark Text Solver ===")
        self.start_timer()
        if not self.wait_for_page_ready():
            return False
        self.click_correction()
        time.sleep(1)
        if not self.click_solution():
            return False
        time.sleep(2)
        group_positions, all_positions = self.extract_correct_positions()
        if not group_positions and not all_positions:
            return False
        self.step(f"Extracted {len(group_positions)} group positions: {group_positions}")
        self.step(f"Extracted {len(all_positions)} word positions: {all_positions}")

        if self.answer_db is not None:
            question_text = self._extract_question_text()
            self.answer_db[question_text] = {
                "type": "mark-text",
                "group_positions": group_positions,
                "all_positions": all_positions,
                "timestamp": datetime.now().isoformat()
            }
            self.step(f"Saved mark text positions to DB")

        self.click_repeat()
        time.sleep(1.5)
        for approach in ["group", "all"]:
            positions = group_positions if approach == "group" else all_positions
            if not positions:
                continue
            self.logger.info(f"--- Trying approach: {approach} ({len(positions)} clicks) ---")
            for iteration in range(self.MAX_ITERATIONS):
                score = self.get_result_score()
                self.logger.info(f"--- Iteration {iteration+1}/{self.MAX_ITERATIONS} | Score: {score} ---")
                if score >= 51:
                    elapsed = self.stop_timer()
                    self.logger.info(f"SUCCESS! Time: {elapsed}")
                    return True
                self.click_by_positions(positions)
                time.sleep(1)
                self.click_correction()
                time.sleep(1)
            if self.get_result_score() >= 51:
                return True
            self.click_repeat()
            time.sleep(1.5)
        return self.get_result_score() >= 51

    def extract_correct_positions(self):
        try:
            result = self.page.evaluate("""
                () => {
                    const allWords = document.querySelectorAll('.exercise-items .word');
                    const groupPositions = [];
                    const allPositions = [];
                    const seenMarkTexts = new Set();
                    allWords.forEach((w, i) => {
                        const mt = w.closest('.mark-text');
                        if (mt) {
                            allPositions.push(i);
                            if (!seenMarkTexts.has(mt)) {
                                seenMarkTexts.add(mt);
                                groupPositions.push(i);
                            }
                        }
                    });
                    return { groupPositions, allPositions };
                }
            """)
            group_positions = result.get("groupPositions", []) if result else []
            all_positions = result.get("allPositions", []) if result else []
            return group_positions, all_positions
        except Exception as e:
            self.logger.error(f"extract_correct_positions error: {e}")
            return [], []

    def click_by_positions(self, positions):
        try:
            self.page.evaluate("""
                () => {
                    const allWords = document.querySelectorAll('.exercise-items .word');
                    allWords.forEach(w => {
                        const cls = (w.className || '').toLowerCase();
                        if (cls.includes('selected') || cls.includes('marked') ||
                            cls.includes('highlight') || cls.includes('chosen') ||
                            cls.includes('active')) {
                            try { w.click(); } catch(e) {}
                        }
                    });
                }
            """)
            time.sleep(0.3)
            clicked = self.page.evaluate("""
                (positions) => {
                    const allWords = Array.from(
                        document.querySelectorAll('.exercise-items .word')
                    );
                    let clickedCount = 0;
                    positions.forEach(idx => {
                        if (idx < allWords.length) {
                            const el = allWords[idx];
                            try {
                                el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                                el.click();
                                clickedCount++;
                            } catch(e) {}
                        }
                    });
                    return clickedCount;
                }
            """, positions)
            self.step(f"Clicked {clicked} words by position")
        except Exception as e:
            self.logger.error(f"click_by_positions error: {e}")

class PronunciationSolver(BaseSolver):
    RECORD_DURATION = 8
    PASSING_SCORE = 51
    MAX_CORRECTION_ROUNDS = 2
    _audio_url_cache = []
    _console_done = False
    _net_done = False

    def solve(self):
        self.logger.info("=== Pronunciation Solver ===")
        self.start_timer()
        self._install_console_listener()
        self._install_network_sniffer()
        self._handle_audio_setup_modal()

        if not self.wait_for_page_ready():
            self.logger.warning("[PRON] Page not ready, skipping pronunciation exercise")
            return True
            
        items = self.page.locator(".exercise-items .item").all()
        if not items:
            self.logger.warning("[PRON] No pronunciation items found, skipping")
            return True
            
        for idx, item in enumerate(items):
            try:
                self._record_item(item, idx)
            except Exception as e:
                self.logger.error(f"[PRON] Error recording item {idx+1}: {e}")
                continue

        self._check_pronunciation_batch()
        for round_number in range(1, self.MAX_CORRECTION_ROUNDS + 1):
            try:
                score = self.get_result_score()
                failed_indexes, unknown_indexes = self._pronunciation_item_statuses()
                self.step(
                    f"Result: {score}/100 | Failed items: "
                    f"{len(failed_indexes)} | Unscored: {len(unknown_indexes)}"
                )
                if score >= self.PASSING_SCORE:
                    elapsed = self.stop_timer()
                    self.logger.info(
                        f"Pronunciation SUCCESS! Time: {elapsed} | Score: {score} "
                        f"(yellow/green accepted, no redo)"
                    )
                    return True
            except Exception as e:
                self.logger.error(f"[PRON] Error in correction round {round_number}: {e}")
                break

            retry_indexes = failed_indexes
            if not retry_indexes and score < self.PASSING_SCORE:
                retry_indexes = unknown_indexes or list(range(len(items)))
            if not retry_indexes:
                break

            self.logger.info(
                f"[PRON] Red result ({score}) — retry round {round_number}: "
                f"items {[idx + 1 for idx in retry_indexes]}"
            )
            for idx in retry_indexes:
                try:
                    self._record_item(items[idx], idx)
                except Exception as e:
                    self.logger.error(f"[PRON] Error retrying item {idx+1}: {e}")
            try:
                self._check_pronunciation_batch()
            except Exception as e:
                self.logger.error(f"[PRON] Error checking batch: {e}")

        try:
            score = self.get_result_score()
            failed_indexes, _ = self._pronunciation_item_statuses()
            self.step(f"Final detected score: {score}")
            if score >= self.PASSING_SCORE:
                elapsed = self.stop_timer()
                self.logger.info(f"Pronunciation SUCCESS! Time: {elapsed} | Score: {score}")
                return True
        except Exception as e:
            self.logger.error(f"[PRON] Error getting final score: {e}")

        self.logger.warning(
            f"[PRON] Pronunciation completed with issues. Score: {score if 'score' in locals() else 'unknown'} | "
            f"Failed items: {[idx + 1 for idx in failed_indexes] if 'failed_indexes' in locals() else 'unknown'} | "
            f"Continuing to next exercise..."
        )
        return True

    def _check_pronunciation_batch(self):
        self.step("Checking pronunciation batch...")
        if self.click_correction():
            time.sleep(2)
        else:
            self.logger.warning("[PRON] Could not click [Correction]")

    def _pronunciation_item_statuses(self):
        try:
            result = self.page.evaluate("""
                () => {
                    const failedIndexes = [];
                    const unknownIndexes = [];
                    const items = Array.from(
                        document.querySelectorAll('.exercise-items .item')
                    );
                    const colorKind = (value) => {
                        const match = value.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/i);
                        if (!match) return null;
                        const red = Number(match[1]);
                        const green = Number(match[2]);
                        const blue = Number(match[3]);
                        if (green > red + 25 && green > blue + 15) return 'pass';
                        if (red > green + 35 && red > blue + 25) return 'fail';
                        if (red > 140 && green > 130 && blue < 130) return 'pass';
                        if (Math.max(red, green, blue) - Math.min(red, green, blue) < 20 &&
                            red > 80 && red < 210) return 'fail';
                        return null;
                    };

                    items.forEach((item, index) => {
                        const result = item.querySelector('.word-result');
                        if (!result) {
                            unknownIndexes.push(index);
                            return;
                        }
                        const nodes = [result, ...result.querySelectorAll('*')];
                        let status = null;
                        for (const node of nodes) {
                            const cls = (node.className || '').toString().toLowerCase();
                            if (/\\b(green|success|correct|passed)\\b/.test(cls)) {
                                status = 'pass';
                                break;
                            }
                            if (/\\b(red|error|wrong|incorrect|failed)\\b/.test(cls)) {
                                status = 'fail';
                                break;
                            }
                            const style = window.getComputedStyle(node);
                            const rect = node.getBoundingClientRect();
                            if (rect.width <= 0 || rect.height <= 0) continue;
                            const background = style.backgroundColor;
                            if (background !== 'rgba(0, 0, 0, 0)' &&
                                background !== 'transparent') {
                                status = colorKind(background);
                                if (status) break;
                            }
                        }
                        if (status === 'fail') failedIndexes.push(index);
                        else if (!status) unknownIndexes.push(index);
                    });
                    return { failedIndexes, unknownIndexes };
                }
            """)
            if result:
                return result.get("failedIndexes", []), result.get("unknownIndexes", [])
        except Exception as e:
            self.logger.debug(f"[PRON] Could not read item results: {e}")
        return [], list(range(self.page.locator(".exercise-items .item").count()))

    def _load_item_reference_audio(self, item):
        start_idx = len(PronunciationSolver._audio_url_cache)
        resource_urls_before = set(self._audio_resource_urls())
        try:
            play_button = item.locator("button.play-button").first
            if play_button.count() > 0:
                play_button.scroll_into_view_if_needed()
                play_button.click(timeout=3000, force=True)
        except Exception as e:
            self.logger.debug(f"[PRON] Reference audio click failed: {e}")

        page_audio_urls = []
        new_resource_urls = []
        network_urls = []
        for _ in range(10):
            time.sleep(0.5)
            page_audio_urls = self._page_audio_urls()
            new_resource_urls = [
                url for url in self._audio_resource_urls()
                if url not in resource_urls_before
            ]
            network_urls = PronunciationSolver._audio_url_cache[start_idx:]
            if network_urls or new_resource_urls or page_audio_urls:
                break

        urls = network_urls[-1:] or new_resource_urls[-1:] or page_audio_urls[-1:]
        return self._fetch_audio_as_base64(urls) if urls else []

    def _page_audio_urls(self):
        try:
            urls = self.page.evaluate("""
                () => Array.from(document.querySelectorAll(
                        'audio[src], audio source[src], [data-audio-url]'
                    )).map(el => el.src || el.getAttribute('data-audio-url'))
                      .filter(Boolean)
            """)
            return list(dict.fromkeys(
                u for u in (urls or []) if isinstance(u, str) and u.startswith('http')
            ))
        except Exception as e:
            self.logger.debug(f"[PRON] Page audio lookup failed: {e}")
            return []

    def _audio_resource_urls(self):
        try:
            urls = self.page.evaluate("""
                () => performance.getEntriesByType('resource').map(entry => entry.name)
                    .filter(name => /(?:\\.mp3|\\.ogg|\\.m4a|\\.wav|\\.opus|\\.aac|\\.webm)(?:[?#]|$)|(?:audio|media|speech|track)/i.test(name))
            """)
            return list(dict.fromkeys(
                url for url in (urls or []) if isinstance(url, str) and url.startswith('http')
            ))
        except Exception as e:
            self.logger.debug(f"[PRON] Resource audio lookup failed: {e}")
            return []

    def _install_console_listener(self):
        if PronunciationSolver._console_done:
            return
        try:
            def on_console(msg):
                try:
                    text = msg.text
                    if '[Bot-Pron]' in text:
                        pass
                except Exception:
                    pass
            self.page.on("console", on_console)
            PronunciationSolver._console_done = True
        except Exception as e:
            self.logger.error(f"Console listener: {e}")

    def _install_network_sniffer(self):
        if PronunciationSolver._net_done:
            return
        try:
            context = self.page.context
            def on_response(response):
                try:
                    url = response.url
                    if url.startswith(('blob:', 'data:')):
                        return
                    url_low = url.lower()
                    is_audio = any(
                        url_low.endswith(ext) or ext + '?' in url_low
                        for ext in ['.mp3', '.ogg', '.m4a', '.wav', '.opus']
                    )
                    if not is_audio and '/track/' in url_low:
                        is_audio = True
                    if not is_audio:
                        try:
                            content_type = (response.header_value('content-type') or '').lower()
                            is_audio = content_type.startswith('audio/')
                        except Exception:
                            pass
                    if is_audio:
                        if url not in PronunciationSolver._audio_url_cache:
                            PronunciationSolver._audio_url_cache.append(url)
                except Exception:
                    pass
            context.on("response", on_response)
            PronunciationSolver._net_done = True
        except Exception as e:
            self.logger.error(f"Net sniffer: {e}")

    def _trigger_audio_load(self):
        try:
            play_btns = self.page.locator("button.play-button").all()
            for pb in play_btns:
                try:
                    if pb.count() > 0 and pb.is_visible(timeout=1000):
                        pb.scroll_into_view_if_needed()
                        time.sleep(0.2)
                        pb.click(timeout=3000, force=True)
                        time.sleep(3)
                except Exception:
                    continue
        except Exception as e:
            self.logger.debug(f"Trigger error: {e}")

    def _fetch_audio_as_base64(self, urls):
        results = []
        for url in urls:
            try:
                headers = {
                    'User-Agent': (
                        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                        'AppleWebKit/537.36 (KHTML, like Gecko) '
                        'Chrome/120.0.0.0 Safari/537.36'
                    ),
                    'Referer': 'https://portal.speexx.com/',
                }
                resp = requests.get(url, headers=headers, timeout=15)
                if resp.status_code == 200:
                    b64 = base64.b64encode(resp.content).decode('ascii')
                    results.append(b64)
            except Exception:
                pass
        return results

    def _set_b64_in_window(self, b64_list):
        try:
            self.page.evaluate("""
                (b64list) => {
                    window.__botPronB64 = b64list;
                    window.__botPronDuration = 0;
                }
            """, b64_list)
        except Exception as e:
            self.logger.error(f"Set b64 error: {e}")

    def _is_mic_recording(self, mic):
        try:
            if mic.get_attribute("disabled") is not None:
                return True
            cls = (mic.get_attribute("class") or "").lower()
            if "recording" in cls or "active" in cls:
                return True
            aria = (mic.get_attribute("aria-pressed") or "").lower()
            if aria == "true":
                return True
        except Exception:
            pass
        return False

    def _record_item(self, item, idx):
        try:
            self.step(f"Recording item {idx + 1}...")
            b64_list = self._load_item_reference_audio(item)
            if not b64_list:
                self.logger.warning(f"[PRON] No reference audio found for item {idx + 1}")
            self._set_b64_in_window(b64_list)
            try:
                self.page.evaluate("""
                    () => {
                        try {
                            if (window.__botPronCtx) {
                                window.__botPronCtx.close();
                                window.__botPronCtx = null;
                            }
                        } catch(e) {}
                    }
                """)
            except Exception:
                pass
            time.sleep(0.2)
            mic = item.locator("button.microphone-button").first
            if mic.count() == 0:
                self.step("No mic button")
                return

            self._handle_audio_setup_modal()

            mic.scroll_into_view_if_needed()
            mic.click(timeout=5000)
            self.step("Mic started")

            popup_closed = False
            for _ in range(12):
                if self._handle_audio_setup_modal():
                    popup_closed = True
                    break
                if self._is_mic_recording(mic):
                    break
                time.sleep(0.2)

            if popup_closed:
                time.sleep(0.4)
                try:
                    if not self._is_mic_recording(mic):
                        mic.click(timeout=5000, force=True)
                        self.step("Mic started after closing Audio setup")
                except Exception as e:
                    self.logger.debug(
                        f"[PRON] mic restart after modal failed: {e}")
            else:
                self.logger.info("[PRON] Audio setup popup not found or already closed.")

            try:
                reference_duration = float(self.page.evaluate(
                    "() => Number(window.__botPronDuration || 0)"
                ) or 0)
            except Exception:
                reference_duration = 0
            record_duration = self.RECORD_DURATION
            if reference_duration > 0:
                record_duration = max(3.5, min(reference_duration + 1.5, 15.0))
            
            adjusted_record_duration = max(1.5, record_duration / AUDIO_SPEED_MULTIPLIER)
            
            self.step(f"Recording duration: {adjusted_record_duration:.1f}s")
            time.sleep(adjusted_record_duration)
            mic.click(timeout=3000, force=True)
            self.step("Mic stopped")
            time.sleep(0.5 / AUDIO_SPEED_MULTIPLIER)
            return True
        except Exception as e:
            self.logger.error(f"Record error: {e}")
            return False

class ExerciseEngine:
    def __init__(self, page, logger, answer_db=None):
        self.page = page
        self.logger = logger
        self.answer_db = answer_db if answer_db is not None else {}
        self.solvers = {
            "type-drag-drop": DragDropSolver,
            "type-drag-drop-table": DragDropSolver,
            "type-answer": AnswerSolver,
            "type-scrambled-sentence": ScrambledSentenceSolver,
            "type-scrambled-table": ScrambledTableSolver,
            "type-single-choice": SingleChoiceSolver,
            "type-multiple-choice": MultipleChoiceSolver,
            "type-video": VideoSolver,
            "type-picture-choice": PictureChoiceSolver,
            "type-toggle-solution": ToggleSolutionSolver,
            "type-mark-text": MarkTextSolver,
            "type-single-item-pronunciation": PronunciationSolver,
            "type-multi-item-pronunciation": PronunciationSolver,
            "type-multiple-item-pronunciation": PronunciationSolver,
        }

    def _detect_type_structurally(self):
        try:
            def visible_count(selector):
                try:
                    loc = self.page.locator(selector)
                    total = 0
                    for idx in range(min(loc.count(), 20)):
                        try:
                            if loc.nth(idx).is_visible(timeout=300):
                                total += 1
                        except Exception:
                            continue
                    return total
                except Exception:
                    return 0

            if visible_count(".scrambled-sentence .scrambled-block") > 0:
                return "type-scrambled-sentence"
            if visible_count(".scrambled-table") > 0:
                return "type-scrambled-table"
            if visible_count("button.microphone-button") > 0:
                return "type-single-item-pronunciation"
            if visible_count(".draggable-container .drag-drop") > 0 or \
                    visible_count(".drag-drop-placeholder") > 0:
                return "type-drag-drop"
            if visible_count("input.gap, .gap-container input") > 0:
                return "type-answer"
        except Exception as e:
            self.logger.debug(f"[TYPE] structural detection failed: {e}")
        return "unknown"

    def _log_unknown_type(self, reason):
        try:
            details = self.page.evaluate("""
                () => {
                    const ex = document.querySelector('.exercise');
                    const count = sel => document.querySelectorAll(sel).length;
                    return {
                        url: location.href,
                        exerciseClasses: ex ? Array.from(ex.classList) : [],
                        markers: {
                            scrambled: count('.scrambled-sentence .scrambled-block'),
                            scrambledTable: count('.scrambled-table'),
                            mic: count('button.microphone-button'),
                            drag: count('.drag-drop, .drag-drop-placeholder'),
                            gap: count('input.gap, .gap-container input'),
                            radio: count('input[type=radio]'),
                            checkbox: count('input[type=checkbox]'),
                            video: count('.video'),
                        },
                        text: (document.body.innerText || '').slice(0, 240)
                    };
                }
            """)
            self.logger.warning(
                f"[TYPE] unknown exercise ({reason}): {details}"
            )
        except Exception as e:
            self.logger.warning(
                f"[TYPE] unknown exercise ({reason}); DOM dump failed: {e}"
            )

    def detect_type(self):
        try:
            exercise_div = self.page.locator(".exercise").first
            video_div = self.page.locator(".video").first
            if exercise_div.count() == 0:
                if video_div.count() > 0:
                    return "type-video"
                fallback = self._detect_type_structurally()
                if fallback != "unknown":
                    self.logger.info(
                        f"[TYPE] Detected {fallback} without .exercise wrapper")
                    return fallback
                self._log_unknown_type("no .exercise element")
                return "unknown"
            cls = exercise_div.get_attribute("class") or ""

            if "type-intro" in cls:
                return "type-intro"

            if "type-drag-drop" in cls:
                if "layout-table" in cls or "layout-grid-duo" in cls:
                    return "type-drag-drop-table"
                return "type-drag-drop"

            if "type-scrambled-sentence" in cls:
                return "type-scrambled-sentence"
            if "type-scrambled-table" in cls:
                return "type-scrambled-table"
            if "type-picture-choice" in cls:
                return "type-picture-choice"
            if "type-toggle-solution" in cls:
                return "type-toggle-solution"
            if "type-single-choice" in cls:
                return "type-single-choice"
            if "type-multiple-choice" in cls:
                return "type-multiple-choice"
            if "type-mark-text" in cls:
                return "type-mark-text"
            if "type-single-item-pronunciation" in cls:
                return "type-single-item-pronunciation"
            if "type-multi-item-pronunciation" in cls:
                return "type-multi-item-pronunciation"
            if "type-multiple-item-pronunciation" in cls:
                return "type-multiple-item-pronunciation"
            if "type-answer" in cls:
                return "type-answer"

            fallback = self._detect_type_structurally()
            if fallback != "unknown":
                self.logger.info(
                    f"[TYPE] Detected {fallback} from markup "
                    f"(wrapper class: {cls!r})")
                return fallback
            self._log_unknown_type(f"unrecognised wrapper class {cls!r}")
            return "unknown"
        except Exception as e:
            self.logger.warning(f"[TYPE] detection error: {e}")
            return "unknown"

    def get_solver(self, t, force_solve=False):
        sc = self.solvers.get(t)
        if not sc:
            return None
        solver = sc(self.page, self.logger, answer_db=self.answer_db)
        solver.force_solve = force_solve
        return solver

class TeeLogger:
    def __init__(self, filename, original_stdout):
        self.terminal = original_stdout
        self.log = open(filename, "a", encoding="utf-8")
        self.lock = threading.Lock()

    def write(self, message):
        with self.lock:
            try:
                self.terminal.write(message)
            except Exception:
                pass
            try:
                clean = re.sub(r'\x1b\[[0-9;]*m', '', message)
                self.log.write(clean)
                self.log.flush()
            except Exception:
                pass

    def flush(self):
        with self.lock:
            try:
                self.terminal.flush()
            except Exception:
                pass
            try:
                self.log.flush()
            except Exception:
                pass

    def close(self):
        with self.lock:
            try:
                if not self.log.closed:
                    self.log.flush()
                    self.log.close()
            except Exception:
                pass

class SpeexxBotCLI:
    MAX_NEW_EXERCISE_CHAIN = 100

    def __init__(self):
        self.config_folder = "config"
        self.logs_folder = "logs"
        self.dump_folder = "webpagedump"
        self.reports_folder = "solve data"
        self.profiles_folder = "chrome_profiles"
        self.accounts_file = os.path.join(self.config_folder, "accounts.json")
        self.portal_state_file = os.path.join(self.config_folder, "portal_state.json")

        self.current_chapter_cid = None
        self.current_chapter_name = ""
        self.current_packet_id = None
        self.results_article_id = None
        self.answer_bank_file = ""

        self.lang = "th"
        self.T = TRANSLATIONS[self.lang]

        self.ensure_folders()
        self.setup_logging()

        self.accounts = self.load_accounts()
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.active_profile_dir = None
        self.current_chapters = []
        self.force_solve_all = False
        self.answer_db = {}
       
        self.answer_records = []
        self.answer_page_contexts = {}
        self.chapter_catalog = []
        self._title_chapter_map = None

    def _input(self, prompt="", sensitive=False):
        value = input(prompt)
        try:
            self.logger.info(f"INPUT: {'<redacted>' if sensitive else value}")
        except Exception:
            pass
        return value

    def _load_answer_db(self, chapter_name):
        if not chapter_name:
            return
        self._title_chapter_map = None
        safe_name = re.sub(r'[\\/*?:"<>|]', "", chapter_name).strip().replace(" ", "_")
        self.answer_bank_file = os.path.join(self.reports_folder, f"answers_{safe_name}.json")

        try:
            if os.path.exists(self.answer_bank_file):
                with open(self.answer_bank_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

                self.answer_page_contexts = {}
                if isinstance(data, dict) and data.get("schema_version") == 2:
                    self.answer_records = data.get("exercises", [])
                    self.chapter_catalog = data.get("chapter", {}).get("catalog", []) or []
                    self.answer_db = {}
                    for exercise in self.answer_records:
                        for page in exercise.get("pages", []):
                            question = page.get("question", "")
                            answer = page.get("answer")
                            if question and isinstance(answer, dict):
                                self.answer_db[question] = answer
                    if self._merge_legacy_answer_bank(safe_name):
                        self.answer_db = {}
                        for exercise in self.answer_records:
                            for page in exercise.get("pages", []):
                                question = page.get("question", "")
                                answer = page.get("answer")
                                if question and isinstance(answer, dict):
                                    self.answer_db[question] = answer
                    loaded_count = len(self.answer_db)
                    total_exercises = len(self.answer_records)
                    total_pages = sum(
                        len(x.get("pages", [])) for x in self.answer_records
                    )
                    self.logger.info(
                        "[DB] Loaded structured database: %s exercises, %s pages, %s answers",
                        total_exercises,
                        total_pages,
                        loaded_count,
                    )
                    self.validate_and_cleanup_answer_db()
                    if hasattr(self, "print_status"):
                        self.print_status(
                            "DB",
                            f"ฐานข้อมูล {chapter_name}: "
                            f"{total_exercises} แบบฝึกหัด, "
                            f"{total_pages} หน้า, "
                            f"ตอบแล้ว {loaded_count} หน้า",
                        )
                else:
                   
                    self.answer_records = []
                    self.chapter_catalog = []
                    self.answer_db = data if isinstance(data, dict) else {}
                    self.logger.info(
                        "[DB] Loaded legacy database: %s answers (will migrate to schema v2)",
                        len(self.answer_db),
                    )
            else:
                self.answer_db = {}
                self.answer_records = []
                self.answer_page_contexts = {}
                self.chapter_catalog = []
                self.logger.info(f"[DB] New answer bank for {chapter_name}")
        except Exception as e:
            self.logger.error(f"[DB] Load failed: {e}")
            self.answer_db = {}
            self.answer_records = []
            self.answer_page_contexts = {}
            self.chapter_catalog = []

    def _merge_legacy_answer_bank(self, safe_name):
        legacy_path = os.path.join("reports", f"answers_{safe_name}.json")
        if not os.path.exists(legacy_path):
            return 0
        try:
            if os.path.abspath(legacy_path) == os.path.abspath(self.answer_bank_file):
                return 0
            with open(legacy_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not (isinstance(data, dict) and data.get("schema_version") == 2):
                return 0
        except Exception as e:
            self.logger.warning(f"[DB] legacy merge skipped: {e}")
            return 0

        def norm_title(title):
            return re.sub(r"\s+", " ", (title or "").strip()).casefold()

        def page_number(page):
            try:
                return int(page.get("page") or 0)
            except Exception:
                return 0

        by_title = {
            norm_title(e.get("title")): e
            for e in self.answer_records if isinstance(e, dict)
        }
        added_exercises = 0
        added_pages = 0
        for legacy_exercise in data.get("exercises", []) or []:
            if not isinstance(legacy_exercise, dict):
                continue
            key = norm_title(legacy_exercise.get("title"))
            target = by_title.get(key)
            if target is None:
                target = {
                    "title": legacy_exercise.get("title") or "Unknown exercise",
                    "pages_total": legacy_exercise.get("pages_total") or 0,
                    "pages": [],
                }
                self.answer_records.append(target)
                by_title[key] = target
                added_exercises += 1
            else:
                target["pages_total"] = max(
                    int(target.get("pages_total") or 0),
                    int(legacy_exercise.get("pages_total") or 0),
                )

            target_pages = {
                page_number(p): p
                for p in target.get("pages", []) if isinstance(p, dict)
            }
            for legacy_page in legacy_exercise.get("pages", []) or []:
                if not isinstance(legacy_page, dict):
                    continue
                number = page_number(legacy_page)
                current = target_pages.get(number)
                if current is None:
                    target.setdefault("pages", []).append(dict(legacy_page))
                    target_pages[number] = target["pages"][-1]
                    added_pages += 1
                    continue
                for field in ("type", "question", "prompt"):
                    if not current.get(field) and legacy_page.get(field):
                        current[field] = legacy_page[field]
                current_answer = current.get("answer")
                legacy_answer = legacy_page.get("answer")
                if legacy_answer and (
                    not current_answer or
                    str(legacy_page.get("updated_at") or "") >
                    str(current.get("updated_at") or "")
                ):
                    current["answer"] = legacy_answer
                    current["updated_at"] = (
                        legacy_page.get("updated_at") or current.get("updated_at")
                    )

        catalog_by_title = {
            norm_title(item.get("title")): item
            for item in self.chapter_catalog if isinstance(item, dict)
        }
        exercise_titles = {
            norm_title(e.get("title"))
            for e in self.answer_records if isinstance(e, dict)
        }
        for item in data.get("chapter", {}).get("catalog", []) or []:
            if not isinstance(item, dict):
                continue
            key = norm_title(item.get("title"))
            if key not in exercise_titles and key not in catalog_by_title:
                continue
            current = catalog_by_title.get(key)
            if current is None:
                self.chapter_catalog.append(dict(item))
                catalog_by_title[key] = self.chapter_catalog[-1]
            else:
                current["pages_total"] = max(
                    int(current.get("pages_total") or 0),
                    int(item.get("pages_total") or 0),
                )

        if added_exercises or added_pages:
            self.logger.info(
                "[DB] Merged legacy answer bank: +%s exercises, +%s pages",
                added_exercises, added_pages,
            )
        return added_exercises + added_pages

    def _save_answer_db_per_chapter(self, chapter_name):
        if not chapter_name:
            return
        safe_name = re.sub(r'[\\/*?:"<>|]', "", chapter_name).strip().replace(" ", "_")
        self.answer_bank_file = os.path.join(self.reports_folder, f"answers_{safe_name}.json")

        try:
            now = datetime.now().isoformat()
            exercises = self.answer_records if isinstance(self.answer_records, list) else []

            def get_exercise(title):
                title = title or "Unknown exercise"
                for exercise in exercises:
                    if exercise.get("title") == title:
                        return exercise
                exercise = {
                    "title": title,
                    "pages_total": 0,
                    "pages": [],
                }
                exercises.append(exercise)
                return exercise

            legacy_exercise = next(
                (e for e in exercises
                 if e.get("title") == "Imported answers (legacy)"),
                None,
            )
            if legacy_exercise is not None:
                visited_questions = set(self.answer_page_contexts)
                legacy_exercise["pages"] = [
                    p for p in legacy_exercise.get("pages", [])
                    if p.get("question") not in visited_questions
                ]

            for question, context in self.answer_page_contexts.items():
                entry = self.answer_db.get(question)
                if not isinstance(entry, dict):
                    continue
                exercise = get_exercise(context.get("title"))
                exercise["pages_total"] = max(
                    int(exercise.get("pages_total") or 0),
                    int(context.get("pages_total") or 0),
                )
                page_number = int(context.get("page") or 0)
                page_record = next(
                    (p for p in exercise.get("pages", [])
                     if p.get("page") == page_number),
                    None,
                )
                if page_record is None:
                    page_record = {"page": page_number}
                    exercise.setdefault("pages", []).append(page_record)
                page_record.update({
                    "type": context.get("type", "unknown"),
                    "question": question,
                    "prompt": context.get("prompt", ""),
                    "answer": entry,
                    "updated_at": now,
                })
                catalog_item = next(
                    (item for item in self.chapter_catalog
                     if item.get("title") == exercise.get("title")),
                    None,
                )
                if catalog_item is not None:
                    catalog_item["pages_total"] = max(
                        int(catalog_item.get("pages_total") or 0),
                        int(context.get("pages_total") or 0),
                    )

            represented = {
                p.get("question")
                for exercise in exercises
                for p in exercise.get("pages", [])
            }
            legacy = get_exercise("Imported answers (legacy)")
            for question, entry in self.answer_db.items():
                if question in represented or not isinstance(entry, dict):
                    continue
                legacy["pages"].append({
                    "page": None,
                    "type": entry.get("type", "unknown"),
                    "question": question,
                    "prompt": "",
                    "answer": entry,
                    "updated_at": now,
                })
                represented.add(question)
            if not legacy.get("pages"):
                exercises[:] = [e for e in exercises if e is not legacy]

            exercises.sort(key=lambda e: (e.get("title") == "Imported answers (legacy)", e.get("title", "")))
            if not self.chapter_catalog:
                self.chapter_catalog = [
                    {"title": e.get("title", ""), "pages_total": e.get("pages_total", 0)}
                    for e in exercises
                    if e.get("title") != "Imported answers (legacy)"
                ]
            total_pages = sum(int(item.get("pages_total") or 0) for item in self.chapter_catalog)
            answered_pages = sum(len(e.get("pages", [])) for e in exercises)
            known_exercises = len(self.chapter_catalog)
            payload = {
                "schema_version": 2,
                "chapter": {
                    "name": chapter_name,
                    "cid": self.current_chapter_cid,
                    "updated_at": now,
                    "summary": {
                        "exercises": known_exercises,
                        "pages": total_pages,
                        "answered_pages": answered_pages,
                    },
                    "catalog": self.chapter_catalog,
                },
                "exercises": exercises,
            }
            with open(self.answer_bank_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            self.answer_records = exercises
            summary = payload["chapter"]["summary"]
            self.logger.info(
                "[DB] Saved structured database: %s exercises, %s pages, %s answered pages",
                summary["exercises"], summary["pages"], summary["answered_pages"],
            )
        except Exception as e:
            self.logger.error(f"[DB] Save failed: {e}")

    def _capture_answer_page_context(self, title, page_number, total_pages,
                                     exercise_type, solver):
        try:
            question = solver._extract_question_text()
            if not question or question == "Unknown Question":
                return
            prompt = ""
            instruction = self.page.locator(".instructions-text").first
            if instruction.count() > 0:
                prompt = instruction.inner_text().strip()
            header = self.page.locator("h1.exercise-header").first
            page_title = header.inner_text().strip() if header.count() > 0 else ""
            self.answer_page_contexts[question] = {
                "title": title or page_title or "Unknown exercise",
                "page": page_number,
                "pages_total": total_pages,
                "type": exercise_type,
                "prompt": prompt,
            }
        except Exception as e:
            self.logger.debug(f"[DB] Page context capture failed: {e}")

    def _ensure_all_exercises_visible(self):
        try:
            max_attempts = 5
            for attempt in range(max_attempts):
                btn = None
                try:
                    loc = self.page.locator('div.show-more[aria-label="แสดงเพิ่มเติม"], div.show-more[aria-label="Show more"]')
                    total = loc.count()
                    for i in range(total):
                        item = loc.nth(i)
                        try:
                            if item.is_visible(timeout=500):
                                btn = item
                                break
                        except Exception:
                            continue
                except Exception:
                    pass

                if btn is None:
                    self.logger.info("[EXPAND] No visible 'Show more' button")
                    return True

                try:
                    btn.click(timeout=3000, force=True)
                    time.sleep(1.0)
                    self.logger.info(f"[EXPAND] Clicked 'Show more' #{attempt+1}")
                except Exception as e:
                    self.logger.error(f"[EXPAND] Click failed: {e}")
                    return False

            return True
        except Exception as e:
            self.logger.error(f"[EXPAND] Error: {e}")
            return False

    def ensure_folders(self):
        for f in [self.config_folder, self.logs_folder,
                  self.dump_folder, self.reports_folder, self.profiles_folder]:
            if not os.path.exists(f):
                os.makedirs(f)

    def validate_and_cleanup_answer_db(self):
        try:
            if not hasattr(self, 'answer_db') or not self.answer_db:
                return
            
            db_path = os.path.join(self.config_folder, f"answers_{self.current_level}.json")
            if not os.path.exists(db_path):
                return
            
            backup_path = db_path + f".backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            shutil.copy2(db_path, backup_path)
            
            original_count = len(self.answer_db)
            cleaned_db = {}
            seen_questions = set()
            removed_duplicates = 0
            removed_corrupted = 0
            
            for question, entry in self.answer_db.items():
                if question in seen_questions:
                    removed_duplicates += 1
                    continue
                
                if not isinstance(entry, dict):
                    removed_corrupted += 1
                    continue
                
                required_fields = ["type", "answer"]
                if not all(field in entry for field in required_fields):
                    removed_corrupted += 1
                    continue
                
                if not entry.get("answer"):
                    removed_corrupted += 1
                    continue
                
                seen_questions.add(question)
                cleaned_db[question] = entry
            
            self.answer_db = cleaned_db
            
            total_removed = removed_duplicates + removed_corrupted
            if total_removed > 0:
                self.logger.info(f"[DB CLEANUP] Removed {removed_duplicates} duplicates, {removed_corrupted} corrupted entries. "
                               f"Total: {original_count} -> {len(cleaned_db)}")
                self._save_answer_db()
            else:
                try:
                    os.remove(backup_path)
                except Exception:
                    pass
                    
        except Exception as e:
            self.logger.error(f"[DB CLEANUP] Error: {e}")

    def _save_answer_db(self):
        try:
            db_path = os.path.join(self.config_folder, f"answers_{self.current_level}.json")
            with open(db_path, "w", encoding="utf-8") as f:
                json.dump(self.answer_db, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.error(f"[DB SAVE] Error: {e}")

    def setup_logging(self):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_filename = os.path.join(self.logs_folder, f"speexx_{ts}.log")
        logging.basicConfig(
            filename=self.log_filename,
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
            encoding='utf-8'
        )
        self.logger = logging.getLogger(__name__)
        self.logger.info("Program started")
        self.logger.info(f"Log: {self.log_filename}")
        self.original_stdout = sys.stdout
        sys.stdout = TeeLogger(self.log_filename, self.original_stdout)

    def clear_screen(self):
        try:
            os.system('cls' if os.name == 'nt' else 'clear')
        except Exception:
            pass

    def print_header(self, clear=True):
        if clear:
            self.clear_screen()
        print("""                
   .d888b,?88,.d88b, d8888b d8888b?88,  88P?88,  88P
   ?8b,   `?88'  ?88d8b_,dPd8b_,dP `?8bd8P' `?8bd8P'
     `?8b   88b  d8P88b    88b     d8P?8b,  d8P?8b, 
  `?888P'   888888P'`?888P'`?888P'd8P' `?8bd8P' `?8b
            88P'                                    
           d88                      Make by Zentrary               
           ?8P                                      
""")

    def print_status(self, t, msg):
        prefix_map = {
            "INFO": f"{Fore.CYAN}[INFO]{Style.RESET_ALL}",
            "OK":   f"{Fore.GREEN}[ OK ]{Style.RESET_ALL}",
            "ERROR":f"{Fore.RED}[ERR ]{Style.RESET_ALL}",
            "WAIT": f"{Fore.YELLOW}[WAIT]{Style.RESET_ALL}",
            "PAGE": f"{Fore.MAGENTA}[PAGE]{Style.RESET_ALL}",
            "RESULT": f"{Fore.WHITE}[>>  ]{Style.RESET_ALL}",
            "MENU": f"{Fore.CYAN}[MENU]{Style.RESET_ALL}",
            "SKIP": f"{Fore.YELLOW}[SKIP]{Style.RESET_ALL}",
            "DB": f"{Fore.BLUE}[ DB ]{Style.RESET_ALL}",
        }
        prefix = prefix_map.get(t, "[?]")
        print(f"  {prefix} {msg}")
        self.logger.info(f"{t}: {msg}")

    def _cleanup_browser(self):
        try:
            if self.context:
                try:
                    self.context.close()
                except BaseException:
                    pass
        except BaseException:
            pass
        try:
            if self.playwright:
                try:
                    self.playwright.stop()
                except BaseException:
                    pass
        except BaseException:
            pass
        self.context = None
        self.browser = None
        self.playwright = None
        self.page = None

    def _install_pronunciation_init(self):
        if getattr(self, '_pron_init_installed', False):
            return
        try:
            init_js = r"""
                (() => {
                    if (window.__botPronInstalled) return;
                    window.__botPronInstalled = true;
                    window.__botPronB64 = [];
                    const origGUM = navigator.mediaDevices.getUserMedia.bind(
                        navigator.mediaDevices
                    );
                    function b64ToArrayBuffer(b64) {
                        const bin = atob(b64);
                        const len = bin.length;
                        const bytes = new Uint8Array(len);
                        for (let i = 0; i < len; i++) {
                            bytes[i] = bin.charCodeAt(i);
                        }
                        return bytes.buffer;
                    }
                    navigator.mediaDevices.getUserMedia = async (constraints) => {
                        if (!constraints || !constraints.audio) {
                            return origGUM(constraints);
                        }
                        try {
                            const AC = window.AudioContext || 
                                       window.webkitAudioContext;
                            const ctx = new AC();
                            await ctx.resume();
                            const dest = ctx.createMediaStreamDestination();
                            for (let i = 0; i < window.__botPronB64.length; i++) {
                                try {
                                    const b64 = window.__botPronB64[i];
                                    const buf = b64ToArrayBuffer(b64);
                                    const audioBuffer = await ctx.decodeAudioData(buf);
                                    const src = ctx.createBufferSource();
                                    src.buffer = audioBuffer;
                                    src.connect(dest);
                                    window.__botPronDuration = audioBuffer.duration || 0;
                                    src.start(ctx.currentTime + 0.35);
                                    window.__botPronCtx = ctx;
                                    return dest.stream;
                                } catch(e) {}
                            }
                            return origGUM(constraints);
                        } catch(e) {
                            return origGUM(constraints);
                        }
                    };
                })();
            """
            self.context.add_init_script(script=init_js)
            self._pron_init_installed = True
            self.logger.info("[PRON-INIT] Installed at context level")
        except Exception as e:
            self.logger.error(f"[PRON-INIT] Failed: {e}")

    def _account_key(self, email):
        return hashlib.sha256(email.strip().lower().encode('utf-8')).hexdigest()[:20]

    def _profile_dir(self, email):
        return os.path.join(self.profiles_folder, self._account_key(email))

    def load_accounts(self):
        if not os.path.exists(self.accounts_file):
            return []
        try:
            with open(self.accounts_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            self.logger.warning(f"[ACCOUNTS] Cannot read accounts file: {e}")
            return []
        if isinstance(data, dict):
            migrated = []
            for user, pwd in data.items():
                migrated.append({
                    "key": self._account_key(user),
                    "email": user,
                    "password": pwd,
                })
            self._save_accounts_raw(migrated)
            return migrated
        return data if isinstance(data, list) else []

    def _save_accounts_raw(self, accounts):
        parent = os.path.dirname(os.path.abspath(self.accounts_file))
        os.makedirs(parent, exist_ok=True)
        temp_name = None
        try:
            if os.path.exists(self.accounts_file):
                shutil.copy2(self.accounts_file, self.accounts_file + ".bak")
            fd, temp_name = tempfile.mkstemp(
                prefix="accounts_", suffix=".tmp", dir=parent
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(accounts, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_name, self.accounts_file)
            return True
        except Exception as e:
            self.logger.error(f"[ACCOUNTS] Save failed: {e}")
            if temp_name:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
            return False

    def save_account(self, email=None, password=None):
        if email is None:
            self.print_header()
            print(f"  {Fore.CYAN}--- {self.T['add_account']} ---{Style.RESET_ALL}")
            try:
                import getpass
                email = self._input(f"  {Fore.CYAN}[>] Username: {Style.RESET_ALL}").strip()
                password = getpass.getpass(
                    f"  {Fore.CYAN}[>] Password: {Style.RESET_ALL}"
                ).strip()
            except (EOFError, KeyboardInterrupt):
                return False
            if not email or not password:
                self.print_status("ERROR", self.T['invalid_choice'])
                self._input(f"\n  {Fore.CYAN}[>] {self.T['press_enter']}{Style.RESET_ALL}")
                return False
        email = email.strip()
        accounts = self.load_accounts()
        key = self._account_key(email)
        existing = next((a for a in accounts if a.get("key") == key), None)
        entry = {"key": key, "email": email}
        if password:
            entry["password"] = password
        elif existing and existing.get("password"):
            entry["password"] = existing["password"]
        accounts = [a for a in accounts if a.get("key") != key]
        accounts.insert(0, entry)
        self.accounts = accounts
        return self._save_accounts_raw(accounts)

    def get_saved_password(self, email):
        accounts = self.load_accounts()
        key = self._account_key(email)
        for a in accounts:
            if a.get("key") == key and a.get("password"):
                return a.get("password")
        return None

    def delete_account_password(self, email):
        accounts = self.load_accounts()
        key = self._account_key(email)
        for a in accounts:
            if a.get("key") == key:
                a.pop("password", None)
        self.accounts = accounts
        return self._save_accounts_raw(accounts)

    def delete_account(self, email):
        accounts = self.load_accounts()
        key = self._account_key(email)
        accounts = [a for a in accounts if a.get("key") != key]
        self.accounts = accounts
        return self._save_accounts_raw(accounts)

    def select_account(self):
        self.print_header()
        print(f"  {Fore.CYAN}--- {self.T['select_account']} ---{Style.RESET_ALL}\n")
        if not self.accounts:
            print(f"  {Fore.YELLOW}[!] {self.T['no_accounts']}{Style.RESET_ALL}")
            try:
                u = self._input(f"\n  {Fore.CYAN}[>] Username: {Style.RESET_ALL}").strip()
                if u:
                    return u, None
            except (EOFError, KeyboardInterrupt):
                pass
            return None, None
        for i, acc in enumerate(self.accounts, 1):
            has_pwd = f" {Fore.GREEN}[saved pwd]{Style.RESET_ALL}" if acc.get("password") else ""
            prof_dir = self._profile_dir(acc.get("email", ""))
            has_prof = f" {Fore.CYAN}[profile]{Style.RESET_ALL}" if os.path.exists(prof_dir) else ""
            print(f"  {Fore.WHITE}[{i}]{Style.RESET_ALL} {acc.get('email', '')}{has_pwd}{has_prof}")
        print(f"  {Fore.WHITE}[N]{Style.RESET_ALL} {self.T['add_account']}")
        print(f"  {Fore.WHITE}[D]{Style.RESET_ALL} {self.T['delete_pwd']}")
        print(f"  {Fore.WHITE}[X]{Style.RESET_ALL} {self.T['delete_acc']}")
        print(f"  {Fore.WHITE}[0]{Style.RESET_ALL} {self.T['back']}")
        try:
            choice = self._input(f"\n  {Fore.CYAN}[>] {self.T['select_account']}: {Style.RESET_ALL}").strip()
        except (EOFError, KeyboardInterrupt):
            return None, None
        if choice == "":
            return None, None
        if choice == "0":
            return None, None
        if choice.lower() == "n":
            try:
                email = self._input(f"\n  {Fore.CYAN}[>] Email: {Style.RESET_ALL}").strip()
            except (EOFError, KeyboardInterrupt):
                return None, None
            if not email:
                return None, None
            return email, None
        if choice.lower() == "d":
            try:
                email = self._input(f"\n  {Fore.CYAN}[>] Email: {Style.RESET_ALL}").strip()
            except (EOFError, KeyboardInterrupt):
                return None, None
            if email:
                self.delete_account_password(email)
                self.accounts = self.load_accounts()
                self.print_status("OK", self.T['deleted_pwd'].format(email))
            self._input(f"\n  {Fore.CYAN}[>] {self.T['press_enter']}{Style.RESET_ALL}")
            return None, None
        if choice.lower() == "x":
            try:
                email = self._input(f"\n  {Fore.CYAN}[>] Email: {Style.RESET_ALL}").strip()
            except (EOFError, KeyboardInterrupt):
                return None, None
            if email:
                self.delete_account(email)
                self.accounts = self.load_accounts()
                self.print_status("OK", self.T['deleted_acc'].format(email))
            self._input(f"\n  {Fore.CYAN}[>] {self.T['press_enter']}{Style.RESET_ALL}")
            return None, None
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(self.accounts):
                acc = self.accounts[idx]
                return acc.get("email", ""), acc.get("password")
        except ValueError:
            pass
        self.print_status("ERROR", self.T['invalid_choice'])
        time.sleep(1)
        return None, None

    def start_browser_and_login(self, u, p):
        self.print_status("INFO", "Starting Browser with persistent profile...")
        self._cleanup_browser()
        try:
            self.results_article_id = None
            self.playwright = sync_playwright().start()
            profile_dir = self._profile_dir(u)
            os.makedirs(profile_dir, exist_ok=True)
            self.active_profile_dir = profile_dir
            self.print_status("INFO", f"Chrome Profile: {profile_dir}")

            self.context = self.playwright.chromium.launch_persistent_context(
                user_data_dir=os.path.abspath(profile_dir),
                headless=False,
                channel="chrome",
                viewport={"width": 1366, "height": 768},
                permissions=["microphone"],
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--window-size=1366,768",
                    "--window-position=0,0",
                    "--use-fake-ui-for-media-stream",
                    "--use-fake-device-for-media-stream",
                    "--autoplay-policy=no-user-gesture-required",
                ],
            )
            self.browser = self.context.browser
            if self.context.pages:
                self.page = self.context.pages[0]
            else:
                self.page = self.context.new_page()

            self._install_pronunciation_init()

            self.page.goto("https://portal.speexx.com/",
                           wait_until="domcontentloaded")
            time.sleep(2)
            if self.check_speexx_session():
                self.print_status("OK", "Session ยังใช้ได้ — ข้ามการ Login")
                return True
            if not p:
                p = self.get_saved_password(u)
                if p:
                    self.print_status("OK", "ใช้รหัสผ่านที่บันทึกไว้")
            if not p:
                try:
                    import getpass
                    p = getpass.getpass(f"  {Fore.CYAN}[>] Password ของ {u}: {Style.RESET_ALL}").strip()
                except (EOFError, KeyboardInterrupt):
                    return False
                if p:
                    try:
                        save = self._input(f"  {Fore.CYAN}[>] บันทึกรหัสผ่าน? (y/n): {Style.RESET_ALL}").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        save = "n"
                    if save == "y":
                        self.save_account(u, p)
                        self.print_status("OK", "บันทึกรหัสผ่านแล้ว")
            if not p:
                self.print_status("ERROR", "ไม่มีรหัสผ่าน")
                return False
            return self._do_login(u, p)
        except Exception as e:
            self.print_status("ERROR", f"Browser Error: {str(e)}")
            self._cleanup_browser()
            self._input(f"\n  {Fore.CYAN}[>] {self.T['press_enter']}{Style.RESET_ALL}")
            return False

    def check_speexx_session(self):
        try:
            url = self.page.url.lower()
            if "login" in url:
                return False
            if self.page.locator("#userName").count() > 0:
                try:
                    if self.page.locator("#userName").first.is_visible(timeout=2000):
                        return False
                except Exception:
                    pass
            if "dashboard" in url or "articles" in url or "portal.speexx.com" in url:
                try:
                    self.page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass
                if self.page.locator("#userName").count() == 0:
                    return True
            return False
        except Exception:
            return False

    def _do_login(self, u, p):
        try:
            self.print_status("WAIT", "Logging in...")
            self.page.goto("https://portal.speexx.com/",
                           wait_until="domcontentloaded")
            time.sleep(1)
            self.page.locator("#userName").wait_for(state="visible", timeout=15000)
            self.page.locator("#userName").fill(u)
            self.page.locator("#button-next").click()
            self.page.locator("#password").wait_for(state="visible", timeout=15000)
            self.page.locator("#password").fill(p)
            try:
                cb = self.page.locator("#rememberMe")
                if cb.is_visible(timeout=2000):
                    cb.check()
                    self.print_status("OK", "Checked Keep me logged in")
            except Exception:
                pass
            self.page.locator("#button-sign-in").click()
            try:
                self.page.wait_for_url(re.compile(r".*(dashboard|articles).*"), timeout=30000)
                self.print_status("OK", "Login Successful.")
                self.save_account(u, p)
                return True
            except Exception:
                self.print_status("ERROR", "Login timeout.")
                self._input(f"\n  {Fore.CYAN}[>] {self.T['press_enter']}{Style.RESET_ALL}")
                return False
        except Exception as e:
            self.print_status("ERROR", f"Login Error: {str(e)}")
            self._input(f"\n  {Fore.CYAN}[>] {self.T['press_enter']}{Style.RESET_ALL}")
            return False

    def _portal_state_key(self):
        try:
            if self.active_profile_dir:
                return os.path.basename(os.path.normpath(self.active_profile_dir))
        except Exception:
            pass
        return "default"

    def _load_portal_state(self):
        try:
            if os.path.exists(self.portal_state_file):
                with open(self.portal_state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            self.logger.warning(f"[PORTAL] load state failed: {e}")
        return {}

    def _write_portal_state(self, state):
        try:
            os.makedirs(self.config_folder, exist_ok=True)
            with open(self.portal_state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.warning(f"[PORTAL] save state failed: {e}")

    def _remember_results_article_id(self, article_id):
        if not article_id:
            return
        self.results_article_id = str(article_id)
        state = self._load_portal_state()
        ids = state.get("results_article_ids")
        if not isinstance(ids, dict):
            ids = {}
        ids[self._portal_state_key()] = self.results_article_id
        state["results_article_ids"] = ids
        state["updated_at"] = datetime.now().isoformat()
        self._write_portal_state(state)
        self.logger.info(f"[PORTAL] Results article id saved: {self.results_article_id}")

    def _cached_results_article_id(self):
        if self.results_article_id:
            return str(self.results_article_id)
        ids = self._load_portal_state().get("results_article_ids")
        if isinstance(ids, dict):
            value = ids.get(self._portal_state_key())
            if value:
                self.results_article_id = str(value)
                return self.results_article_id
        return None

    def _forget_results_article_id(self, article_id=None):
        try:
            if article_id is None or (
                self.results_article_id and
                str(article_id) == str(self.results_article_id)
            ):
                self.results_article_id = None
            state = self._load_portal_state()
            ids = state.get("results_article_ids")
            if isinstance(ids, dict):
                key = self._portal_state_key()
                current = ids.get(key)
                if article_id is None or (current and str(current) == str(article_id)):
                    ids.pop(key, None)
                    state["results_article_ids"] = ids
                    self._write_portal_state(state)
        except Exception:
            pass

    @staticmethod
    def _results_article_id_from_url(url):
        try:
            match = re.search(r"/articles/(\d+)/results(?:\b|[/?#])", url or "", re.I)
            return match.group(1) if match else None
        except Exception:
            return None

    @staticmethod
    def _article_id_from_exercise_url(url):
        try:
            match = re.search(
                r"/articles/(\d+)/(?:standard|pronunciation|vocabulary)-packet/",
                url or "", re.I
            )
            return match.group(1) if match else None
        except Exception:
            return None

    def _scan_article_links(self):
        results_ids, other_ids = [], []
        try:
            hrefs = self.page.eval_on_selector_all(
                "a[href]", "els => els.map(el => el.href)"
            )
        except Exception:
            hrefs = []
        for href in hrefs or []:
            match = re.search(r"/articles/(\d+)(/[^?#]*)?", str(href or ""), re.I)
            if not match:
                continue
            article_id = match.group(1)
            rest = (match.group(2) or "").lower()
            if "/results" in rest:
                if article_id not in results_ids:
                    results_ids.append(article_id)
            elif not re.search(
                r"/(?:standard|pronunciation|vocabulary)-packet|/packet|/exercise",
                rest,
            ):
                if article_id not in results_ids and article_id not in other_ids:
                    other_ids.append(article_id)
        if not results_ids:
            try:
                html = self.page.content()
                for match in re.finditer(r"/articles/(\d+)/results", html, re.I):
                    article_id = match.group(1)
                    if article_id not in results_ids:
                        results_ids.append(article_id)
            except Exception:
                pass
        return results_ids, other_ids

    def _history_results_article_ids(self, limit=15):
        article_ids = []
        if not self.active_profile_dir:
            return article_ids
        history_path = os.path.join(self.active_profile_dir, "Default", "History")
        if not os.path.exists(history_path):
            return article_ids
        tmp_path = os.path.join(
            tempfile.gettempdir(),
            f"speexx_history_{self._portal_state_key()}.db",
        )
        try:
            shutil.copy2(history_path, tmp_path)
            import sqlite3
            con = sqlite3.connect(tmp_path)
            try:
                rows = con.execute(
                    "SELECT url FROM urls "
                    "WHERE url LIKE '%/articles/%/results%' "
                    "ORDER BY last_visit_time DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            finally:
                con.close()
            for (url,) in rows:
                article_id = self._results_article_id_from_url(url)
                if article_id and article_id not in article_ids:
                    article_ids.append(article_id)
        except Exception as e:
            self.logger.debug(f"[PORTAL] history scan skipped: {e}")
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
        return article_ids

    def _results_candidates(self, exclude=None):
        excluded = {str(x) for x in (exclude or [])}
        candidates = []

        def add(article_id):
            article_id = str(article_id) if article_id else None
            if article_id and article_id not in excluded and article_id not in candidates:
                candidates.append(article_id)

        add(self._results_article_id_from_url(self.page.url))
        add(self._article_id_from_exercise_url(self.page.url))

        add(self._cached_results_article_id())

        results_ids, other_ids = self._scan_article_links()
        for article_id in results_ids:
            add(article_id)
        for article_id in other_ids:
            add(article_id)

        for article_id in self._history_results_article_ids():
            add(article_id)
        return candidates

    def _refresh_portal_links_and_scan(self, exclude=None):
        for url in (
            "https://portal.speexx.com/",
            "https://portal.speexx.com/articles/list",
        ):
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=20000)
                time.sleep(1)
                if self._results_candidates(exclude):
                    return True
            except Exception as e:
                self.logger.warning(f"[PORTAL] open {url} failed: {e}")
        return bool(self._results_candidates(exclude))

    def _results_page_ready(self, timeout=15000):
        try:
            if "/articles/list" in self.page.url.lower():
                return False
            self.page.locator(".level-container").first.wait_for(
                state="visible", timeout=timeout
            )
            return True
        except Exception:
            return False

    def _open_results_candidate(self, article_id):
        url = f"https://portal.speexx.com/articles/{article_id}/results"
        self.logger.info(f"[PORTAL] Trying Results URL: {url}")
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=20000)
        except Exception as nav_e:
            if "interrupted by another navigation" not in str(nav_e):
                self.logger.warning(f"[PORTAL] goto failed: {nav_e}")
        time.sleep(1)
        if "/articles/list" in self.page.url.lower():
            self.logger.info(
                f"[PORTAL] article {article_id} ใช้กับบัญชีนี้ไม่ได้ "
                "(ถูกพาไป /articles/list)"
            )
            return False, True
        if not self._results_page_ready():
            self.logger.info(
                f"[PORTAL] article {article_id} ไม่พบหน้า Results "
                f"(url={self.page.url})"
            )
            return False, False
        final_id = self._results_article_id_from_url(self.page.url) or article_id
        self._remember_results_article_id(final_id)
        return True, False

    def _check_and_fix_redirect(self):
        try:
            if "/articles/list" not in self.page.url.lower():
                return False
            self.print_status(
                "WAIT",
                "ตรวจพบการเปลี่ยนเส้นทางไปที่หน้า /articles/list — กำลังค้นหา article id "
                "ของหน้า Results สำหรับบัญชีนี้...",
            )
            return self.navigate_to_results()
        except Exception as e:
            self.logger.error(f"[REDIRECT] Error fixing redirect: {e}")
            return False

    def navigate_to_results(self):
        if not self.page:
            self.print_status("ERROR", "Browser not started.")
            self._input(f"\n  {Fore.CYAN}[>] {self.T['press_enter']}{Style.RESET_ALL}")
            return False
        self.print_status("INFO", "Navigating to Results...")
        tried = set()
        try:
            for _ in range(6):
                candidates = self._results_candidates(tried)
                if not candidates:
                    if not self._refresh_portal_links_and_scan(tried):
                        break
                    candidates = self._results_candidates(tried)
                if not candidates:
                    break
                article_id = candidates[0]
                self.logger.info(
                    f"[PORTAL] Results candidates ({self._portal_state_key()}): "
                    f"{candidates}"
                )
                ok, wrong_account = self._open_results_candidate(article_id)
                if ok:
                    try:
                        self.page.locator("tbody tr.clickable").first.wait_for(
                            state="attached", timeout=8000
                        )
                    except Exception:
                        self.logger.debug("[NAV] Exercise table not ready yet")
                    self.print_status("OK", "Results loaded.")
                    return True
                tried.add(article_id)
                if wrong_account:
                    self._forget_results_article_id(article_id)
            self.print_status(
                "ERROR",
                "Failed: หา article id ของหน้า Results สำหรับบัญชีนี้ไม่พบ "
                "(ลองหาจากหน้า Dashboard และ /articles/list แล้ว)",
            )
        except Exception as e:
            self.print_status("ERROR", f"Failed: {str(e)}")
        self._input(f"\n  {Fore.CYAN}[>] {self.T['press_enter']}{Style.RESET_ALL}")
        return False

    def fetch_and_select_chapter(self):
        if not self.page:
            return
        self.print_header()
        print(f"  {Fore.CYAN}--- {self.T['available_chapters']} ---{Style.RESET_ALL}\n")
        try:
            current_url = self.page.url.lower()
            if "/results" not in current_url or "/articles/list" in current_url:
                self.print_status("INFO", "Navigating to Results page...")
                if not self.navigate_to_results():
                    return
                time.sleep(1)

            self.page.locator(".level-container").first.wait_for(
                state="visible", timeout=30000
            )
            chapters = self.page.locator(".level-container").all()
            self.current_chapters = []
            for ch in chapters:
                box = ch.locator(".level-inside-box")
                cls = box.get_attribute("class") or ""
                if "future" in cls:
                    continue
                name = box.locator(".level-text").inner_text().strip()

                if "test" in name.lower():
                    continue

                cid = ch.get_attribute("data-id")
                self.current_chapters.append((name, cid))
            if not self.current_chapters:
                self.print_status("ERROR", self.T['no_chapters'])
                self._input(f"\n  {Fore.CYAN}[>] {self.T['press_enter']}{Style.RESET_ALL}")
                return
            for i, (n, _) in enumerate(self.current_chapters, 1):
                print(f"  {Fore.WHITE}[{i}]{Style.RESET_ALL} {n}")
            print(f"\n  {Fore.WHITE}[0]{Style.RESET_ALL} {self.T['back']}")
            try:
                c = self._input(f"\n  {Fore.CYAN}[>] {self.T['select_chapter']}{Style.RESET_ALL}").strip()
                if c == "" or c == "0":
                    return
                c_int = int(c)
                if 1 <= c_int <= len(self.current_chapters):
                    n, cid = self.current_chapters[c_int - 1]
                    self.select_chapter(cid, n)
            except ValueError:
                pass
        except Exception as e:
            self.print_status("ERROR", f"Error: {str(e)}")
            self._input(f"\n  {Fore.CYAN}[>] {self.T['press_enter']}{Style.RESET_ALL}")

    def _load_chapter_rows(self, cid):
        try:
            if "/results" not in self.page.url.lower() or "/articles/list" in self.page.url.lower():
                if not self.navigate_to_results():
                    return []

            chapter_box = self.page.locator(
                f".level-container[data-id='{cid}'] .level-inside-box"
            )
            chapter_box.wait_for(state="visible", timeout=15000)
            try:
                chapter_box.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            chapter_box.click(timeout=10000)
            time.sleep(1)

            tab = self.page.locator("#results-table-exercises")
            try:
                tab.scroll_into_view_if_needed(timeout=3000)
                time.sleep(0.4)
                tab.click(timeout=5000)
            except Exception:
                pass
            time.sleep(1)

            self._ensure_all_exercises_visible()
            self.page.locator("tbody tr.clickable").first.wait_for(
                state="visible", timeout=15000
            )
            return self.page.locator("tbody tr.clickable").all()
        except Exception as e:
            self.logger.warning(f"[CHAPTER] load rows failed for cid={cid}: {e}")
            return []

    def _chapter_contains(self, title):
        key = (title or "").strip().casefold()
        if not key:
            return False
        return any(
            (item.get("title") or "").strip().casefold() == key
            for item in (self.chapter_catalog or [])
        )

    def _known_title_chapters(self):
        cached = getattr(self, "_title_chapter_map", None)
        if cached is not None:
            return cached

        mapping = {}

        def add(title, chapter):
            key = (title or "").strip().casefold()
            chapter = (chapter or "").strip()
            if key and chapter:
                mapping.setdefault(key, set()).add(chapter)

        for item in (self.chapter_catalog or []):
            if isinstance(item, dict):
                add(item.get("title"), self.current_chapter_name)

        pattern = os.path.join(self.reports_folder, "answers_*.json")
        for path in glob.glob(pattern):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            if not isinstance(data, dict) or data.get("schema_version") != 2:
                continue
            chapter = (data.get("chapter") or {}).get("name") or ""
            for item in ((data.get("chapter") or {}).get("catalog") or []):
                if isinstance(item, dict):
                    add(item.get("title"), chapter)
            for exercise in (data.get("exercises") or []):
                if isinstance(exercise, dict):
                    add(exercise.get("title"), chapter)

        self._title_chapter_map = mapping
        return mapping

    def _is_outside_current_chapter(self, current_title, new_title):
        if not (self.current_chapter_cid and self.current_chapter_name):
            return False
        if not self._chapter_contains(current_title):
            return False

        key = (new_title or "").strip().casefold()
        if not key:
            return False
        known_chapters = self._known_title_chapters().get(key)
        if not known_chapters:
            return False
        return self.current_chapter_name not in known_chapters

    def _url_packet_id(self):
        try:
            match = re.search(r"/standard-packet/(\d+)", self.page.url)
            return match.group(1) if match else None
        except Exception:
            return None

    def select_chapter(self, cid, name):
        self.print_status("WAIT", f"Selecting {name}...")
        try:
            if "/results" not in self.page.url.lower():
                if not self.navigate_to_results():
                    return

            self.current_chapter_cid = cid
            self.current_chapter_name = name
            self.current_packet_id = None
            self._load_answer_db(name)

            if not self._load_chapter_rows(cid):
                self.print_status("ERROR", f"ไม่พบรายการแบบฝึกหัดของ {name}")
                return

            self.print_status("OK", f"{name} selected.")
            self.fetch_exercises()
        except Exception as e:
            self.print_status("ERROR", f"Error: {str(e)}")
            self._input(f"\n  {Fore.CYAN}[>] {self.T['press_enter']}{Style.RESET_ALL}")

    def _parse_selection(self, s, max_n):
        s = (s or "").strip().lower()
        if not s or s == "all":
            return list(range(1, max_n + 1))
        result = []
        try:
            if ',' in s:
                parts = s.split(',')
                for p in parts:
                    p = p.strip()
                    if '-' in p:
                        a, b = p.split('-')
                        result.extend(range(int(a), int(b) + 1))
                    else:
                        result.append(int(p))
            elif '-' in s:
                a, b = s.split('-')
                result = list(range(int(a), int(b) + 1))
            else:
                result = [int(s)]
        except Exception:
            return []
        seen = set()
        clean = []
        for i in result:
            if 1 <= i <= max_n and i not in seen:
                seen.add(i)
                clean.append(i)
        return clean

    def fetch_exercises(self):
        self.print_header()
        print(f"  {Fore.CYAN}--- {self.T['exercise_list']} ---{Style.RESET_ALL}\n")
        try:
            self._ensure_all_exercises_visible()

            self.page.locator("tbody tr.clickable").first.wait_for(
                state="visible", timeout=15000
            )
            rows = self.page.locator("tbody tr.clickable").all()
            if not rows:
                self.print_status("ERROR", self.T['no_exercises'])
                self._input(f"\n  {Fore.CYAN}[>] {self.T['press_enter']}{Style.RESET_ALL}")
                return
            print(f"  {Fore.CYAN}{'No.':<5}| {self.T['table_title']:<33}| "
                  f"{self.T['table_date']:<13}| {self.T['table_time']:<9}| "
                  f"{self.T['table_score']:<7}| {self.T['table_review']}{Style.RESET_ALL}")
            print(f"  {Fore.CYAN}{'-' * 95}{Style.RESET_ALL}")
            exercises = []
            for i, row in enumerate(rows, 1):
                tds = row.locator("td").all()
                if len(tds) >= 4:
                    title = tds[0].inner_text().strip()
                    date = tds[1].inner_text().strip()
                    t_spent = tds[2].inner_text().strip()
                    score = tds[3].inner_text().strip()
                    rb = ""
                    if len(tds) >= 5:
                        b = tds[4].locator("button").all()
                        if b:
                            rb = b[0].inner_text().strip()
                    exercises.append((title, rb))
                    td = title[:30] + "..." if len(title) > 33 else title
                    score_color = Fore.GREEN if score == "100" else Fore.WHITE
                    print(f"  [{i:<3}]| {td:<33}| {date:<13}| "
                          f"{t_spent:<9}| {score_color}{score:<7}{Style.RESET_ALL}| {rb:<10}")
           
            known_pages = {
                item.get("title"): item.get("pages_total", 0)
                for item in self.chapter_catalog
                if isinstance(item, dict)
            }
            existing_by_key = {
                (item.get("title") or "").strip().casefold(): item
                for item in self.chapter_catalog
                if isinstance(item, dict)
            }
            merged_catalog = []
            seen_keys = set()
            for title, _ in exercises:
                key = (title or "").strip().casefold()
                seen_keys.add(key)
                merged_catalog.append({
                    "title": title,
                    "pages_total": int(known_pages.get(title, 0) or 0),
                })
            for key, item in existing_by_key.items():
                if key and key not in seen_keys:
                    merged_catalog.append({
                        "title": item.get("title"),
                        "pages_total": int(item.get("pages_total") or 0),
                    })
            self.chapter_catalog = merged_catalog
            self._title_chapter_map = None
            self.logger.info(
                "[DB] Chapter catalogue: %s exercises (page totals fill as exercises are opened)",
                len(self.chapter_catalog),
            )
           
            self._save_answer_db_per_chapter(self.current_chapter_name)
            print(f"  {Fore.CYAN}{'-' * 95}{Style.RESET_ALL}")
            print(f"\n  {Fore.WHITE}[0]{Style.RESET_ALL} {self.T['back']}")
            try:
                sel_input = self._input(
                    f"\n  {Fore.CYAN}[>] {self.T['select_exercises']}{Style.RESET_ALL}"
                )
                if sel_input == "" or sel_input.strip() == "0":
                    return

                selected = self._parse_selection(sel_input, len(exercises))
                if not selected:
                    self.print_status("ERROR", self.T['invalid_choice'])
                    self._input(f"\n  {Fore.CYAN}[>] {self.T['press_enter']}{Style.RESET_ALL}")
                    return

                selected_titles = [exercises[idx - 1][0] for idx in selected]

                if len(selected_titles) == 1:
                    self.open_exercise_menu(selected_titles[0])
                else:
                    print(f"\n  {Fore.CYAN}--- เลือกโหมดการทำ ({len(selected)} ข้อ) ---{Style.RESET_ALL}")
                    print(f"  {Fore.WHITE}[1]{Style.RESET_ALL} ทำทีละข้อ")
                    print(f"  {Fore.WHITE}[2]{Style.RESET_ALL} ทำทั้งหมดอัตโนมัติ")
                    print(f"  {Fore.WHITE}[3]{Style.RESET_ALL} {self.T['opt_new_learning_path']}")

                    try:
                        mode = self._input(
                            f"  {Fore.CYAN}[>] เลือกโหมด: {Style.RESET_ALL}"
                        ).strip()
                    except Exception:
                        mode = "1"

                    if mode == "2":
                        self.auto_solve_batch(selected_titles)
                    elif mode == "3":
                        self.start_new_learning_path(selected_titles[0])
                    else:
                        for title in selected_titles:
                            self.open_exercise_menu(title)
                            time.sleep(0.5)

            except ValueError:
                pass
        except Exception as e:
            self.print_status("ERROR", f"Error: {str(e)}")
            self._input(f"\n  {Fore.CYAN}[>] {self.T['press_enter']}{Style.RESET_ALL}")

    def open_exercise_menu(self, title):
        self.print_header()
        print(f"  {Fore.CYAN}--- {self.T['exercise_menu']}: {title} ---{Style.RESET_ALL}\n")
        print(f"  {Fore.WHITE}[1]{Style.RESET_ALL} {self.T['opt_new_learning_path']}")
        print(f"  {Fore.WHITE}[2]{Style.RESET_ALL} {self.T['opt_dump']}")
        print(f"  {Fore.WHITE}[0]{Style.RESET_ALL} {self.T['back']}")
        try:
            c = self._input(f"\n  {Fore.CYAN}[>] Select: {Style.RESET_ALL}").strip()
            if c == "" or c == "0":
                return
            if c == "1":
                self.start_new_learning_path(title)
            elif c == "2":
                self.dump_all_exercise_pages(title)
        except ValueError:
            pass

    def dump_all_exercise_pages(self, title):
        self.print_status("INFO", f"Dumping {title}...")
        try:
            self._ensure_all_exercises_visible()
            row = self.page.locator("tbody tr.clickable").filter(has_text=title).first
            try:
                row.scroll_into_view_if_needed()
                time.sleep(0.3)
            except Exception:
                pass
            row.click(timeout=15000)
            time.sleep(2)
            total = self.get_total_exercises()
            if total == 0:
                self.print_status("ERROR", "Cannot determine total.")
                self._input(f"\n  {Fore.CYAN}[>] {self.T['press_enter']}{Style.RESET_ALL}")
                return
            safe = re.sub(r'[\\/*?:"<>|]', "", title).replace(" ", "_")
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            for i in range(1, total + 1):
                self.navigate_to_exercise_page(i)
                time.sleep(1)
                fn = f"{self.dump_folder}/{safe}_exercise_{i}_{ts}.html"
                with open(fn, "w", encoding="utf-8") as f:
                    f.write(self.page.content())
                self.print_status("OK", f"Dumped {i}/{total}: {fn}")
            self.print_status("OK", "All dumped.")
            self.print_status("INFO", "Returning to exercise list...")
            try:
                self.page.go_back()
                time.sleep(2)
            except Exception:
                pass
            self.navigate_to_results()
            time.sleep(1)
            self.fetch_exercises()
            return
        except Exception as e:
            self.print_status("ERROR", f"Error: {str(e)}")
            self._input(f"\n  {Fore.CYAN}[>] {self.T['press_enter']}{Style.RESET_ALL}")

    def search_answer_interactive(self):
        chapter = (self.current_chapter_name or "B1.2").strip()
        self._load_answer_db(chapter)
        if not self.answer_db:
            self.print_status("ERROR", f"ไม่พบฐานข้อมูลคำตอบของ {chapter}")
            self._input(f"\n  {Fore.CYAN}[>] {self.T['press_enter']}{Style.RESET_ALL}")
            return

        print(f"\n  {Fore.CYAN}--- ค้นหาคำตอบจากโจทย์ ({chapter}) ---{Style.RESET_ALL}")
        print("  วางโจทย์ได้หลายบรรทัด แล้วพิมพ์ END ในบรรทัดสุดท้าย")
        print("  พิมพ์ 0 เพื่อกลับเมนูหลัก\n")
        lines = []
        while True:
            line = self._input("  ")
            if not lines and line.strip() == "0":
                return
            if line.strip() == "END":
                break
            lines.append(line)

        query = "\n".join(lines).strip()
        if not query:
            self.print_status("INFO", "ไม่ได้รับโจทย์")
            return

        def norm(value):
            return " ".join(str(value or "").replace("\xa0", " ").split()).casefold()

        def comparable(value):
            parts = [part for part in str(value).split(" | ")
                     if not re.fullmatch(r"Exercise\s+\d+", part.strip(), re.I)]
            return norm(" | ".join(parts))

        qnorm = comparable(query)
        exact = []
        scored = []
        for key, answer in self.answer_db.items():
            if not isinstance(answer, dict):
                continue
            knorm = comparable(key)
            if qnorm == knorm:
                exact.append((key, answer, 1.0))
                continue
            score = SequenceMatcher(None, qnorm[:1800], knorm[:1800]).ratio()
            if qnorm and (qnorm in knorm or knorm in qnorm):
                score = max(score, 0.92)
            scored.append((score, key, answer))

        matches = exact or [(score, key, answer)
                            for score, key, answer in sorted(
                                scored, key=lambda item: item[0], reverse=True)[:5]
                            if score >= 0.35]
        if not matches:
            self.print_status("INFO", "ไม่พบโจทย์ที่ตรงกันในฐานข้อมูล")
            self._input(f"\n  {Fore.CYAN}[>] {self.T['press_enter']}{Style.RESET_ALL}")
            return

        print(f"\n  {Fore.GREEN}พบคำตอบที่ใกล้เคียง {len(matches)} รายการ{Style.RESET_ALL}")
        for number, item in enumerate(matches, 1):
            if exact:
                key, answer, score = item
            else:
                score, key, answer = item
            print(f"\n  [{number}] ความตรงกัน {score * 100:.1f}%")
            print(f"  โจทย์ฐานข้อมูล: {str(key)[:260]}")
            print(f"  ประเภท: {answer.get('type', 'unknown')}")
            print("  คำตอบ:")
            print(json.dumps(answer.get("answers"), ensure_ascii=False, indent=4))

        self._input(f"\n  {Fore.CYAN}[>] {self.T['press_enter']}{Style.RESET_ALL}")

    def get_total_exercises(self, retries=4, wait_seconds=1.5):
        selectors = [
            ".exercises-large-navigation.visible-lg li",
            ".exercises-large-navigation.visible-md li",
            ".exercises-large-navigation.visible-sm li",
        ]
        for attempt in range(retries):
            try:
                dismiss_audio_setup_modal(self.page, self.logger)
                for sel in selectors:
                    items = self.page.locator(sel).all()
                    if items:
                        self.logger.info(
                            f"[TOTAL] Found {len(items)} pages via {sel} "
                            f"(attempt {attempt+1}/{retries})"
                        )
                        return len(items)
            except Exception as e:
                self.logger.debug(f"[TOTAL] attempt {attempt+1} error: {e}")

            if attempt < retries - 1:
                time.sleep(wait_seconds)

        self.logger.warning(
            f"[TOTAL] Cannot find exercise navigation after {retries} attempts"
        )
        return 0

    def navigate_to_exercise_page(self, num):
        try:
            dismiss_audio_setup_modal(self.page, self.logger)

            items = self.page.locator(".exercises-large-navigation.visible-lg li").all()
            if not items:
                items = self.page.locator(".exercises-large-navigation.visible-md li").all()
            if not items:
                items = self.page.locator(".exercises-large-navigation.visible-sm li").all()
            if not (1 <= num <= len(items)):
                return False

            target = items[num - 1]
            try:
                target.scroll_into_view_if_needed(timeout=1500)
            except Exception:
                pass

            clicked = False
            try:
                target.click(timeout=4000)
                clicked = True
            except Exception as e:
                self.logger.debug(f"[NAV] Page {num} click failed: {str(e)[:120]}")
                dismiss_audio_setup_modal(self.page, self.logger)
                try:
                    target.click(timeout=2500, force=True)
                    clicked = True
                except Exception:
                    try:
                        target.evaluate("el => el.click()")
                        clicked = True
                    except Exception:
                        clicked = False

            time.sleep(0.35)
            if clicked:
                dismiss_audio_setup_modal(self.page, self.logger)
            return clicked
        except Exception:
            return False

    def detect_completion_modal(self):
        try:
            circ = self.page.locator(".circliful-and-results").first
            if circ.count() > 0 and circ.is_visible(timeout=2000):
                return True
            next_btn = self.page.locator("#nextButton").first
            if next_btn.count() > 0 and next_btn.is_visible(timeout=2000):
                return True
            repeat_btn = self.page.locator("button.text-button-custom.repeat").first
            if repeat_btn.count() > 0 and repeat_btn.is_visible(timeout=2000):
                return True
            try:
                body_text = self.page.locator("body").inner_text(timeout=2000)
                if "ขั้นตอน" in body_text and "ทำได้ดีมาก" in body_text:
                    return True
                if "step" in body_text.lower() and "well done" in body_text.lower():
                    return True
            except Exception:
                pass
            return False
        except Exception:
            return False

    def wait_for_completion_modal(self, timeout=20):
        for i in range(timeout * 2):
            if self.detect_completion_modal():
                self.print_status("OK", f"Completion modal detected (after {i*0.5:.1f}s)")
                return True
            time.sleep(0.5)
        self.print_status("WAIT", f"Completion modal NOT detected within {timeout}s")
        return False

    def extract_completion_info(self):
        info = {"score": "N/A", "score_max": "N/A", "level": "N/A",
                "progress": "N/A", "time": "N/A", "title": "N/A"}
        try:
            score_num = self.page.locator(".circliful-container-results .timer .number").first
            if score_num.count() > 0:
                info["score"] = score_num.text_content().strip()
            result_container = self.page.locator(".exercise-result-container").first
            if result_container.count() > 0:
                texts = result_container.locator(".text").all()
                if len(texts) >= 2:
                    info["score"] = texts[0].inner_text().strip()
                    info["score_max"] = texts[1].inner_text().strip()
            level_num = self.page.locator(".circliful-container-progress .timer .number").first
            if level_num.count() > 0:
                info["level"] = level_num.text_content().strip()
            prog_text = self.page.locator(".exercise-percentage-container .text").first
            if prog_text.count() > 0:
                info["progress"] = prog_text.text_content().strip()
            try:
                body_text = self.page.locator("body").inner_text(timeout=2000)
                m = re.search(r"เวลา\s+\d+\s+ชม\.\s+\d+\s+น\.", body_text)
                if m:
                    info["time"] = m.group(0)
                m2 = re.search(r"ขั้นตอน\s+\d+\s*·\s*[^\n]+", body_text)
                if m2:
                    info["title"] = m2.group(0).strip()
                
                if info["time"] == "N/A":
                    m_en = re.search(r"\d+\s+min", body_text)
                    if m_en:
                        info["time"] = m_en.group(0)
                if info["title"] == "N/A":
                    header = self.page.locator("h1.exercise-header").first
                    if header.count() > 0:
                        info["title"] = header.inner_text().strip()
            except Exception:
                pass
        except Exception as e:
            self.logger.error(f"extract_completion_info error: {e}")
        return info

    def click_modal_repeat(self):
        try:
            for sel in ["button.text-button-custom.repeat", "button.repeat",
                        "button:has-text('ทำซ้ำ')", "button:has-text('Repeat')"]:
                btn = self.page.locator(sel).first
                if btn.count() > 0 and btn.is_visible(timeout=2000):
                    btn.click(timeout=5000, force=True)
                    self.print_status("OK", f"Clicked [{self.T['repeat']}]")
                    time.sleep(1.5)
                    return True
            return False
        except Exception as e:
            self.print_status("ERROR", f"Repeat click failed: {e}")
            return False

    def click_modal_continue(self):
        try:
            for sel in ["#nextButton", "button.block-button.btn-primary.next",
                        "button:has-text('เรียนรู้ต่อ')"]:
                btn = self.page.locator(sel).first
                if btn.count() > 0 and btn.is_visible(timeout=2000):
                    btn.click(timeout=5000, force=True)
                    self.print_status("OK", f"Clicked [{self.T['continue']}]")
                    time.sleep(1.5)
                    return True
            return False
        except Exception as e:
            self.print_status("ERROR", f"Continue click failed: {e}")
            return False

    def _print_summary_report(self, results):
        total_time = 0.0
        success_count = 0
        fail_count = 0
        skip_count = 0
        for _, r, _, e in results:
            if r == "SUCCESS":
                success_count += 1
            elif r == "SKIPPED":
                skip_count += 1
            else:
                fail_count += 1
            if e != "N/A":
                try:
                    if "m" in e:
                        parts = e.replace("m", "").replace("s", "").split()
                        total_time += float(parts[0]) * 60 + float(parts[1])
                    else:
                        total_time += float(e.replace("s", ""))
                except Exception:
                    pass
        print()
        print(f"  {Fore.CYAN}{'=' * 75}{Style.RESET_ALL}")
        print(f"  {Fore.GREEN}   >>> {self.T['summary']} <<<{Style.RESET_ALL}")
        print(f"  {Fore.CYAN}{'=' * 75}{Style.RESET_ALL}")
        print(f"  {Fore.CYAN}   {self.T['exercise_list']}:{Style.RESET_ALL}")
        print(f"  {Fore.CYAN}{'-' * 75}{Style.RESET_ALL}")
        for pn, r, s, e in results:
            if r == "SUCCESS":
                icon = f"{Fore.GREEN}[OK]{Style.RESET_ALL}"
                status_text = self.T['pass']
            elif r == "SKIPPED":
                icon = f"{Fore.YELLOW}[--]{Style.RESET_ALL}"
                status_text = self.T['skip']
            else:
                icon = f"{Fore.RED}[X]{Style.RESET_ALL}"
                status_text = self.T['fail']
            print(f"   [{icon}] {self.T['page']} {pn:<2}  |  {status_text:<8}  |  "
                  f"{self.T['score']} {s:>3}/100  |  {self.T['time_spent']} {e}")
        print(f"  {Fore.CYAN}{'-' * 75}{Style.RESET_ALL}")
        print(f"  {Fore.GREEN}   {self.T['passed_count']} : {success_count}{Style.RESET_ALL}")
        if fail_count > 0:
            print(f"  {Fore.RED}   {self.T['failed_count']} : {fail_count}{Style.RESET_ALL}")
        if skip_count > 0:
            print(f"  {Fore.YELLOW}   {self.T['skipped_count']} : {skip_count}{Style.RESET_ALL}")
        print(f"  {Fore.CYAN}   {self.T['total_time']} : {total_time:.2f} s{Style.RESET_ALL}")
        print(f"  {Fore.CYAN}{'=' * 75}{Style.RESET_ALL}")

    def _capture_diagnostic_snapshot(self, title, page_number, exercise_type):
        if not self.page:
            return None
        safe = re.sub(r"[^A-Za-z0-9ก-๙._-]+", "_", title).strip("._") or "exercise"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(
            self.dump_folder,
            f"diagnostic_{safe}_page{page_number}_{exercise_type}_{stamp}.html",
        )
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.page.content())
            self.logger.warning(f"[DIAGNOSTIC] Snapshot saved: {path}")
            return path
        except Exception as e:
            self.logger.warning(f"[DIAGNOSTIC] Snapshot failed: {e}")
            return None

    def handle_completion_modal(self, results, skip_summary=False):
        info = self.extract_completion_info()
        if not skip_summary:
            self._print_summary_report(results)
        print(f"\n  {Fore.CYAN}   Speexx Info:{Style.RESET_ALL}")
        print(f"  {Fore.CYAN}{'-' * 75}{Style.RESET_ALL}")
        if info.get("title", "N/A") != "N/A":
            print(f"   >>> {info['title']}")
        if info.get("score", "N/A") != "N/A":
            print(f"   {self.T['score']} : {info['score']} / {info.get('score_max', '100')}")
        if info.get("level", "N/A") != "N/A":
            print(f"   Level : {info['level']}")
        if info.get("progress", "N/A") != "N/A":
            print(f"   Progress : {info['progress']}")
        if info.get("time", "N/A") != "N/A":
            print(f"   Time : {info['time']}")
        print(f"  {Fore.CYAN}{'=' * 75}{Style.RESET_ALL}")

        print(f"\n  {Fore.CYAN}--- {self.T['what_next']} ---{Style.RESET_ALL}")
        print(f"  {Fore.WHITE}[1]{Style.RESET_ALL} {self.T['repeat']}")
        print(f"  {Fore.WHITE}[2]{Style.RESET_ALL} {self.T['continue']}")
        print(f"  {Fore.WHITE}[0]{Style.RESET_ALL} {self.T['nothing']}")

        while True:
            try:
                c = self._input(f"\n  {Fore.CYAN}[>] เลือก: {Style.RESET_ALL}").strip()
                if c == "1":
                    if self.click_modal_repeat():
                        return "repeat"
                elif c == "2":
                    if self.click_modal_continue():
                        time.sleep(2)
                        try:
                            self.page.wait_for_load_state("domcontentloaded", timeout=5000)
                        except Exception:
                            pass
                        return "continue"
                elif c == "0" or c == "":
                    return "none"
            except (ValueError, KeyboardInterrupt):
                return "none"

    def get_current_exercise_title(self):
        try:
            for sel in [".folders-dropdown .title span",
                        ".instructions-text",
                        "h1.exercise-header"]:
                el = self.page.locator(sel).first
                if el.count() > 0:
                    txt = el.inner_text().strip()
                    if txt:
                        return txt
        except Exception:
            pass
        return "Unknown"

    def _reset_exercise_page(self):
        try:
            for sel in [
                "button.text-button-custom.repeat",
                "button.action-exercise-button.repeat",
                "button.repeat",
                "button:has-text('ทำซ้ำ')",
                "button:has-text('Repeat')",
                "[class*='repeat']",
            ]:
                try:
                    btn = self.page.locator(sel).first
                    if btn.count() > 0 and btn.is_visible(timeout=1500):
                        btn.scroll_into_view_if_needed()
                        time.sleep(0.3 / GLOBAL_SPEED_MULTIPLIER)
                        btn.click(timeout=3000, force=True)
                        self.logger.info(f"[RESET] Clicked repeat via: {sel}")
                        time.sleep(2 / GLOBAL_SPEED_MULTIPLIER)
                        return True
                except Exception:
                    continue
            self.logger.info("[RESET] No repeat button found")
            return False
        except Exception as e:
            self.logger.error(f"[RESET] Error: {e}")
            return False

    def _try_skip_to_next_page(self):
        try:
            for sel in [
                "button.action-exercise-button.next",
                "button:has-text('ถัดไป')",
                "button:has-text('Next')",
                "#nextButton",
            ]:
                try:
                    btn = self.page.locator(sel).first
                    if btn.count() > 0 and btn.is_visible(timeout=2000):
                        btn.scroll_into_view_if_needed()
                        time.sleep(0.3 / GLOBAL_SPEED_MULTIPLIER)
                        btn.click(timeout=5000, force=True)
                        self.logger.info(f"[SKIP] Clicked Next via: {sel}")
                        time.sleep(1.5 / GLOBAL_SPEED_MULTIPLIER)
                        return True
                except Exception:
                    continue
            self.logger.info("[SKIP] No Next button found")
            return False
        except Exception as e:
            self.logger.error(f"[SKIP] Error: {e}")
            return False

    def _parse_elapsed_seconds(self, elapsed):
        if not elapsed or elapsed == "N/A":
            return 0.0
        try:
            if "m" in elapsed:
                parts = elapsed.replace("m", "").replace("s", "").split()
                return float(parts[0]) * 60 + float(parts[1])
            return float(elapsed.replace("s", ""))
        except Exception:
            return 0.0

    def _auto_dismiss_modal(self):
        for sel in [
            ".modal .close",
            "button.close",
            "[aria-label='Close']",
            "[aria-label='ปิด']",
            ".circliful-and-results .close",
        ]:
            try:
                btn = self.page.locator(sel).first
                if btn.count() > 0 and btn.is_visible(timeout=1000):
                    btn.click(timeout=3000, force=True)
                    self.logger.info(f"[BATCH] Dismissed modal via {sel}")
                    return True
            except Exception:
                continue

        for sel in [
            "button.block-button.btn-primary.next",
            "#nextButton",
            "button:has-text('Continue learning')",
            "button:has-text('Continue')",
            "button:has-text('เรียนรู้ต่อ')",
        ]:
            try:
                btn = self.page.locator(sel).first
                if btn.count() > 0 and btn.is_visible(timeout=1000):
                    btn.click(timeout=3000, force=True)
                    self.logger.info(f"[BATCH] Dismissed modal via {sel}")
                    return True
            except Exception:
                continue

        try:
            self.page.keyboard.press("Escape")
            time.sleep(0.5)
            return True
        except Exception:
            pass
        return False

    def _ask_solve_mode(self):
        print(f"\n  {Fore.CYAN}--- เลือกโหมดการทำ ---{Style.RESET_ALL}")
        print(f"  {Fore.WHITE}[1]{Style.RESET_ALL} ทำทุกข้อ "
              f"(force — ทำแม้ได้ 100 แล้ว)")
        print(f"  {Fore.WHITE}[2]{Style.RESET_ALL} ข้ามข้อที่ได้ 100 แล้ว (default)")
        print(f"  {Fore.WHITE}[0]{Style.RESET_ALL} ยกเลิก")
        try:
            c = self._input(
                f"\n  {Fore.CYAN}[>] เลือกโหมด: {Style.RESET_ALL}"
            ).strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if c == "1":
            return True
        if c == "2" or c == "":
            return False
        if c == "0":
            return None
       
        return False

    def start_new_learning_path(self, first_title):
        print(f"\n  {Fore.CYAN}--- {self.T['opt_new_learning_path']} ---{Style.RESET_ALL}")
        print(f"  เริ่มจาก: {first_title}")
        force = self._ask_solve_mode()
        if force is None:
            return
        self.force_solve_all = force
        try:
            confirm = self._input(
                f"\n  {Fore.CYAN}[>] เริ่มทำและ Continue อัตโนมัติ? "
                f"(Enter=yes / 0=ยกเลิก): {Style.RESET_ALL}"
            ).strip()
        except (EOFError, KeyboardInterrupt):
            return
        if confirm == "0":
            return
        self.auto_solve_exercise(
            first_title,
            selected_pages=None,
            is_continuation=False,
            already_opened=False,
            auto_continue=True,
        )

    def _print_batch_summary_report(self, batch_results):
        total_time = 0.0
        success_count = 0
        fail_count = 0
        skip_count = 0

        for _, status, _, elapsed in batch_results:
            if status == "SUCCESS":
                success_count += 1
            elif status == "SKIPPED":
                skip_count += 1
            else:
                fail_count += 1
            total_time += self._parse_elapsed_seconds(elapsed)

        print()
        print(f"  {Fore.CYAN}{'=' * 78}{Style.RESET_ALL}")
        print(f"  {Fore.GREEN}   >>> BATCH SUMMARY — สรุปผลรวมทั้งหมด <<<{Style.RESET_ALL}")
        print(f"  {Fore.CYAN}{'=' * 78}{Style.RESET_ALL}")
        print(f"  {Fore.CYAN}   จำนวนแบบฝึกหัดที่เลือก: {len(batch_results)}{Style.RESET_ALL}")
        print(f"  {Fore.CYAN}{'-' * 78}{Style.RESET_ALL}")

        for title, status, score, elapsed in batch_results:
            if status == "SUCCESS":
                icon = f"{Fore.GREEN}[OK]{Style.RESET_ALL}"
                status_text = "ผ่าน"
            elif status == "SKIPPED":
                icon = f"{Fore.YELLOW}[--]{Style.RESET_ALL}"
                status_text = "ข้าม"
            else:
                icon = f"{Fore.RED}[X]{Style.RESET_ALL}"
                status_text = "ไม่ผ่าน"
            td = title[:36] + "..." if len(title) > 39 else title
            score_color = Fore.GREEN if score >= 51 else Fore.RED
            print(f"   [{icon}] {td:<42} | {status_text:<6} | "
                  f"{score_color}{score:>3}/100{Style.RESET_ALL} | {elapsed}")

        print(f"  {Fore.CYAN}{'-' * 78}{Style.RESET_ALL}")
        print(f"  {Fore.GREEN}   ผ่าน      : {success_count}{Style.RESET_ALL}")
        if fail_count > 0:
            print(f"  {Fore.RED}   ไม่ผ่าน   : {fail_count}{Style.RESET_ALL}")
        if skip_count > 0:
            print(f"  {Fore.YELLOW}   ข้าม      : {skip_count}{Style.RESET_ALL}")
        print(f"  {Fore.CYAN}   เวลารวม   : {total_time:.2f}s{Style.RESET_ALL}")
        print(f"  {Fore.CYAN}{'=' * 78}{Style.RESET_ALL}")

    def auto_solve_batch(self, titles):
        if not titles:
            return

        self.print_header()
        print(f"  {Fore.CYAN}=== BATCH MODE: {len(titles)} แบบฝึกหัด ==={Style.RESET_ALL}\n")
        for i, t in enumerate(titles, 1):
            print(f"  {Fore.WHITE}[{i}]{Style.RESET_ALL} {t}")
        print(f"\n  {Fore.YELLOW}โหมดนี้จะทำทุกข้อให้เสร็จโดยไม่ถาม{Style.RESET_ALL}")

        force = self._ask_solve_mode()
        if force is None:
            return
        self.force_solve_all = force

        try:
            confirm = self._input(
                f"\n  {Fore.CYAN}[>] เริ่มเลย? (Enter=yes / 0=ยกเลิก): {Style.RESET_ALL}"
            ).strip()
            if confirm == "0":
                return
        except Exception:
            return

        batch_results = []

        for idx, title in enumerate(titles, 1):
            self.print_header()
            print(f"  {Fore.CYAN}=== [{idx}/{len(titles)}] {title} ==={Style.RESET_ALL}")

            if idx > 1:
                try:
                    self.navigate_to_results()
                    time.sleep(1.5)
                except Exception:
                    pass

            try:
                status, score, elapsed = self.auto_solve_exercise(
                    title,
                    selected_pages=None,
                    is_continuation=False,
                    already_opened=False,
                    batch_mode=True,
                )
            except Exception as e:
                self.print_status("ERROR", f"Batch error on '{title}': {e}")
                status, score, elapsed = "ERROR", 0, "N/A"

            batch_results.append((title, status, score, elapsed))
            if status == "SUCCESS":
                status_code = "OK"
            elif status == "SKIPPED":
                status_code = "SKIP"
            else:
                status_code = "ERROR"
            self.print_status(
                status_code,
                f"[{idx}/{len(titles)}] {title}: {status} ({score}) | {elapsed}"
            )

        self._print_batch_summary_report(batch_results)

        try:
            self.navigate_to_results()
            time.sleep(1)
        except Exception:
            pass

    def auto_solve_exercise(self, title, selected_pages=None,
                            is_continuation=False, already_opened=False,
                            total_pages=None, batch_mode=False,
                            auto_continue=False, continuation_depth=0,
                            chain_seen_titles=None):
        if not is_continuation and not batch_mode:
            self.print_status("INFO", f"{self.T['auto_solve']}: {title}")
            self.print_status("INFO", self.T['will_not_move'])

        final_status = "FAILED"
        final_score = 0
        final_elapsed = "N/A"
        if auto_continue:
            if chain_seen_titles is None:
                chain_seen_titles = set()
            chain_seen_titles.add((title or "").strip().casefold())

        try:
            if not already_opened and not is_continuation:
                if self.current_chapter_cid:
                    loaded = self._load_chapter_rows(self.current_chapter_cid)
                    if not loaded:
                        self.print_status(
                            "WAIT",
                            f"โหลดรายการบท {self.current_chapter_name} ไม่สำเร็จ — ลองอ่านตารางปัจจุบัน"
                        )
                        self._ensure_all_exercises_visible()
                else:
                    self._ensure_all_exercises_visible()

                clicked = self._click_row_safely(title)
                if not clicked:
                    self.print_status("ERROR", f"Cannot open '{title}' — skipping")
                    if not batch_mode:
                        self._input(f"\n  {Fore.CYAN}[>] {self.T['press_enter']}{Style.RESET_ALL}")
                    return ("ERROR", 0, "N/A")

                self.current_packet_id = self._url_packet_id() or self.current_packet_id

                try:
                    self.page.wait_for_selector(
                        ".exercise, .video, .exercises-large-navigation.visible-lg li",
                        state="visible",
                        timeout=15000
                    )
                except Exception as e:
                    self.logger.warning(f"[WAIT] Exercise page not ready: {str(e)[:80]}")
                time.sleep(1.5)

            dismiss_audio_setup_modal(
                self.page, self.logger,
                lambda msg: self.print_status("OK", msg)
            )

            if total_pages is None:
                total_pages = self.get_total_exercises()

            if total_pages == 0:
                self.logger.warning("[TOTAL] First attempt = 0, retrying with reload...")
                try:
                    self.page.reload(wait_until="domcontentloaded", timeout=15000)
                    time.sleep(2)
                    total_pages = self.get_total_exercises()
                except Exception as e:
                    self.logger.error(f"[TOTAL] Reload failed: {e}")

            if total_pages == 0:
                self.print_status("ERROR", "Cannot determine total.")
                if not batch_mode:
                    self._input(f"\n  {Fore.CYAN}[>] {self.T['press_enter']}{Style.RESET_ALL}")
                return (final_status, final_score, final_elapsed)

            if not batch_mode:
                self.print_status("INFO", self.T['found_pages'].format(total_pages))

            if selected_pages is None:
                pages_to_solve = list(range(1, total_pages + 1))
                is_full = True
            else:
                pages_to_solve = [p for p in selected_pages if 1 <= p <= total_pages]
                is_full = (len(pages_to_solve) == total_pages)

            if not pages_to_solve:
                self.print_status("ERROR", "No valid pages selected.")
                return (final_status, final_score, final_elapsed)

            effective_force = self.force_solve_all

            engine = ExerciseEngine(self.page, self.logger, answer_db=self.answer_db)
            results = []
            all_success = True
            has_skipped = False
            PASS_LINE = 51
            MAX_TIME_PER_PAGE = 300

            for i in pages_to_solve:
                self.navigate_to_exercise_page(i)
                time.sleep(1.0)
                dismiss_audio_setup_modal(self.page, self.logger)
                et = engine.detect_type()
                if et == "unknown":
                    self.logger.warning(f"[TYPE] Page {i}: type unknown — retrying detection")
                    time.sleep(1.0)
                    dismiss_audio_setup_modal(self.page, self.logger)
                    et = engine.detect_type()
                self.print_status("PAGE", f"{self.T['page']} {i}/{total_pages} | {self.T['type']}: {et}")

                if et in UNSUPPORTED_TYPES:
                    display_name = UNSUPPORTED_TYPES[et]
                    if et == "type-intro":
                        self.print_status("SKIP", "ข้ามหน้าแนะนำ (ไม่มีโจทย์ให้ทำ)")
                    else:
                        self.print_status(
                            "SKIP",
                            f"ข้ามข้อนี้: โปรแกรมยังไม่ได้เพิ่มระบบในการทำ"
                            f"แบบฝึกหัดประเภท {display_name} ({et})"
                        )
                    results.append((i, "SKIPPED", 0, "N/A"))
                    has_skipped = True
                    self._try_skip_to_next_page()
                    time.sleep(0.3)
                    continue

                solver = engine.get_solver(et, force_solve=effective_force)
                if not solver:
                    snapshot = self._capture_diagnostic_snapshot(title, i, et)
                    detail = f" Snapshot: {snapshot}" if snapshot else ""
                    self.print_status("ERROR", f"No solver for {et}. Skipping.{detail}")
                    results.append((i, "NO SOLVER", 0, "N/A"))
                    all_success = False
                    continue

                self._capture_answer_page_context(
                    title, i, total_pages, et, solver
                )

                page_pass_line = (
                    100 if et in {"type-drag-drop", "type-drag-drop-table"}
                    else PASS_LINE
                )

                if et == "type-video":
                    try:
                        solver.solve()
                    except Exception as e:
                        self.print_status("ERROR", f"Video solver error: {e}")
                        dump_path = self._capture_diagnostic_snapshot(f"{title}_page{i}_video_error", i, et)
                        if dump_path:
                            self.print_status("INFO", f"Auto-dumped video error page: {dump_path}")
                    elapsed = solver.stop_timer() if solver.start_time else "N/A"
                    self.print_status("RESULT", f"{self.T['page']} {i}: {self.T['success']} (video) | {elapsed}")
                    results.append((i, "SUCCESS", 100, elapsed))
                    try:
                        next_btn = self.page.locator("button.action-exercise-button.next").first
                        if next_btn.count() > 0 and next_btn.is_visible(timeout=1000):
                            next_btn.click(timeout=1200)
                            time.sleep(0.3)
                    except Exception:
                        pass
                    continue

                if not effective_force:
                    try:
                        existing_score = solver.get_result_score()
                    except Exception:
                        existing_score = 0

                    if existing_score >= page_pass_line:
                        self.print_status(
                            "RESULT",
                            f"{self.T['page']} {i}: {self.T['success']} "
                            f"({existing_score}) [already passed]"
                        )
                        results.append((i, "SUCCESS", existing_score, "0.00s"))
                        try:
                            next_btn = self.page.locator("button.action-exercise-button.next").first
                            if next_btn.count() > 0 and next_btn.is_visible(timeout=1000):
                                next_btn.click(timeout=1200)
                                time.sleep(0.3)
                        except Exception:
                            pass
                        continue
                else:
                    self.logger.info(
                        f"[FORCE] Page {i}: force_solve=True — skipping early-pass check"
                    )

                max_retries = 3
                retries = 0
                success = False
                score = 0
                elapsed = "N/A"
                best_score = 0
                page_start_time = time.time()

                while retries < max_retries:
                    if time.time() - page_start_time > MAX_TIME_PER_PAGE:
                        self.print_status(
                            "WAIT",
                            f"Page {i} exceeded {MAX_TIME_PER_PAGE}s. Skipping."
                        )
                        success = False
                        break

                    self.logger.info(
                        f"--- [Attempt {retries + 1}/{max_retries}] "
                        f"Solving page {i} ({et}) ---")
                    try:
                        success = solver.solve()
                    except Exception as e:
                        self.print_status("ERROR", f"Solver error: {str(e)}")
                        dump_path = self._capture_diagnostic_snapshot(f"{title}_page{i}_error", i, et)
                        if dump_path:
                            self.print_status("INFO", f"Auto-dumped error page: {dump_path}")
                        success = False

                    self._capture_answer_page_context(
                        title, i, total_pages, et, solver
                    )

                    if et in {"type-scrambled-table", "type-drag-drop", "type-drag-drop-table", "type-picture-choice", "type-toggle-solution"}:
                        time.sleep(0.5)
                    elif et in {"type-scrambled-sentence", "type-answer", "type-single-choice", "type-multiple-choice"}:
                        time.sleep(0.3)

                    elapsed = solver.stop_timer() if solver.start_time else "N/A"

                    verified_score = solver.get_result_score()
                    self.logger.info(
                        f"[VERIFY] Solver returned '{success}'. "
                        f"Real score from page is: {verified_score}")

                    score = verified_score
                    if score > best_score:
                        best_score = score

                    if score >= page_pass_line:
                        success = True
                        break
                    else:
                        if success:
                            self.logger.warning(
                                f"[VERIFY] Solver claimed success but score "
                                f"{score} < {page_pass_line}. Treating as failure.")
                        
                        retries += 1
                        if retries < max_retries:
                            self.print_status("WAIT", self.T['retrying'].format(retries, max_retries))
                            self._reset_exercise_page()
                            solver = engine.get_solver(et, force_solve=effective_force)
                            self.logger.info(
                                f"[RETRY] Created new solver instance for page {i}.")
                        else:
                            self.print_status("WAIT", f"{self.T['max_retries']} (Score: {best_score})")

                if best_score >= page_pass_line:
                    self.print_status("RESULT", f"{self.T['page']} {i}: {self.T['success']} ({best_score}) | {elapsed}")
                    results.append((i, "SUCCESS", best_score, elapsed))
                    
                    self._save_answer_db_per_chapter(self.current_chapter_name)
                    
                    try:
                        next_btn = self.page.locator("button.action-exercise-button.next").first
                        if next_btn.count() > 0 and next_btn.is_visible(timeout=1000):
                            next_btn.click(timeout=1200)
                            time.sleep(0.3)
                    except Exception:
                        pass
                else:
                    self.print_status("ERROR", f"{self.T['page']} {i}: {self.T['failed']} (Score: {best_score}) | {elapsed}")
                    dump_path = self._capture_diagnostic_snapshot(f"{title}_page{i}_failed", i, et)
                    if dump_path:
                        self.print_status("INFO", f"Auto-dumped failed page: {dump_path}")
                    results.append((i, "FAILED", best_score, elapsed))
                    all_success = False
                    self._try_skip_to_next_page()
                    time.sleep(0.3)

            self._print_summary_report(results)

            final_status = "SUCCESS" if all_success else "FAILED"
            if has_skipped and all((r[1] == "SKIPPED") for r in results):
                final_status = "SKIPPED"

            for _, _, s, _ in results:
                if isinstance(s, (int, float)) and s > final_score:
                    final_score = s
            total_secs = 0.0
            for _, _, _, e in results:
                total_secs += self._parse_elapsed_seconds(e)
            final_elapsed = f"{total_secs:.2f}s"

            self._save_answer_db_per_chapter(self.current_chapter_name)

            if batch_mode:
                if not is_full:
                    self.print_status("INFO", self.T['selected_only'])
                self.print_status("INFO", "Done.")
                try:
                    if self.detect_completion_modal():
                        self._auto_dismiss_modal()
                        time.sleep(1)
                except Exception:
                    pass
                return (final_status, final_score, final_elapsed)

            self.print_status("INFO", self.T['selected_only'] if not is_full else "Done.")
            if not is_full:
                self._input(f"\n  {Fore.CYAN}[>] {self.T['press_enter']}{Style.RESET_ALL}")

            if not is_full:
                return (final_status, final_score, final_elapsed)

            if all_success:
                if self.wait_for_completion_modal(timeout=20):
                    previous_url = self.page.url
                    previous_title = self.get_current_exercise_title()
                    if auto_continue:
                        if continuation_depth >= self.MAX_NEW_EXERCISE_CHAIN:
                            self.print_status(
                                "ERROR",
                                f"หยุด auto-Continue: ถึงขีดจำกัด "
                                f"{self.MAX_NEW_EXERCISE_CHAIN} แบบฝึกหัดแล้ว"
                            )
                            return (final_status, final_score, final_elapsed)
                        action = "continue" if self.click_modal_continue() else "none"
                    else:
                        action = self.handle_completion_modal(results, skip_summary=True)
                    if action == "repeat":
                        self.print_status("INFO", "Restarting packet (Repeat)...")
                        time.sleep(1.5)
                        try:
                            self.navigate_to_exercise_page(1)
                            time.sleep(1)
                        except Exception:
                            pass
                        return self.auto_solve_exercise(
                            title, selected_pages=None,
                            is_continuation=True, already_opened=True,
                            total_pages=total_pages,
                        )
                    elif action == "continue":
                        self.print_status("OK", "Moving on (Continue)...")
                        time.sleep(2)
                        try:
                            if auto_continue:
                                self.page.wait_for_function(
                                    """
                                    (previous) => {
                                        if (window.location.href !== previous.url) return true;
                                        const selectors = [
                                            '.folders-dropdown .title span',
                                            '.instructions-text',
                                            'h1.exercise-header'
                                        ];
                                        return selectors.some(selector => {
                                            const element = document.querySelector(selector);
                                            return element && element.textContent.trim() &&
                                                element.textContent.trim() !== previous.title;
                                        });
                                    }
                                    """,
                                    arg={"url": previous_url, "title": previous_title},
                                    timeout=15000,
                                )
                                if "/results" in self.page.url.lower():
                                    return self._continue_auto_from_results(
                                        title, final_status, final_score,
                                        final_elapsed, chain_seen_titles,
                                        continuation_depth,
                                    )
                            self.page.wait_for_selector(
                                ".exercise, .video",
                                state="visible",
                                timeout=10000
                            )
                            new_total = self.get_total_exercises(
                                retries=2 if auto_continue else 4,
                                wait_seconds=0.75 if auto_continue else 1.5,
                            )
                            new_title = self.get_current_exercise_title()
                            if auto_continue:
                                known_chapters = self._known_title_chapters().get(
                                    (new_title or "").strip().casefold()
                                )
                                if not known_chapters:
                                    self.logger.info(
                                        "[CONTINUE] '%s' is not in any saved catalogue yet "
                                        "(new exercise) — following Continue.",
                                        new_title,
                                    )
                            if auto_continue and new_total == 0:
                                return self._continue_auto_from_results(
                                    title, final_status, final_score,
                                    final_elapsed, chain_seen_titles,
                                    continuation_depth,
                                )
                            if auto_continue and self._is_outside_current_chapter(
                                    previous_title, new_title):
                                self.print_status(
                                    "WAIT",
                                    f"Continue พาไปแบบฝึกหัดนอกบท {self.current_chapter_name} "
                                    f"({new_title}) — กลับไปทำข้อในบทเดิม"
                                )
                                return self._continue_auto_from_results(
                                    title, final_status, final_score,
                                    final_elapsed, chain_seen_titles,
                                    continuation_depth,
                                )
                            if (auto_continue and self.page.url == previous_url and
                                    new_title == previous_title):
                                self.print_status(
                                    "WAIT",
                                    "Continue แล้วหน้า/ชื่อแบบฝึกหัดไม่เปลี่ยน จึงหยุดเพื่อป้องกันวนซ้ำ"
                                )
                                return (final_status, final_score, final_elapsed)
                            self.print_status("OK", f"New exercise loaded: {new_title} ({new_total} pages)")
                            return self.auto_solve_exercise(
                                new_title, selected_pages=None,
                                is_continuation=True, already_opened=True,
                                total_pages=new_total,
                                auto_continue=auto_continue,
                                continuation_depth=continuation_depth + 1,
                                chain_seen_titles=chain_seen_titles,
                            )
                        except Exception as e:
                            if auto_continue:
                                self.logger.warning(
                                    f"[CONTINUE] Direct exercise navigation failed; "
                                    f"trying Results fallback: {e}"
                                )
                                return self._continue_auto_from_results(
                                    title, final_status, final_score,
                                    final_elapsed, chain_seen_titles,
                                    continuation_depth,
                                )
                            self.print_status("WAIT", f"หน้าใหม่ยังไม่โหลด: {e}")
                            return (final_status, final_score, final_elapsed)
                    else:
                        return (final_status, final_score, final_elapsed)
                else:
                    return (final_status, final_score, final_elapsed)
            else:
                return (final_status, final_score, final_elapsed)

        except Exception as e:
            self.print_status("ERROR", f"Auto solve error: {str(e)}")
        finally:
            if not is_continuation and not batch_mode:
                try:
                    self.after_solve_menu()
                except Exception:
                    pass

        return (final_status, final_score, final_elapsed)

    def _next_uncompleted_exercise_title(self, current_title, rows, seen_titles):
        exercises = []
        for row in rows:
            cells = row.locator("td").all()
            if len(cells) < 4:
                continue
            exercise_title = cells[0].inner_text().strip()
            spent_text = cells[2].inner_text().strip()
            score_text = cells[3].inner_text().strip()
            score_match = re.search(r"\d+", score_text)
            score = int(score_match.group()) if score_match else None
            exercises.append({
                "title": exercise_title,
                "spent": spent_text,
                "score": score,
            })

        if not exercises:
            return None

        current_key = (current_title or "").strip().casefold()
        current_index = next(
            (index for index, item in enumerate(exercises)
             if item["title"].casefold() == current_key),
            -1,
        )
        indexes = list(range(current_index + 1, len(exercises)))
        indexes.extend(range(0, max(0, current_index + 1)))

        seen = seen_titles or set()
        for index in indexes:
            item = exercises[index]
            name = item["title"]
            key = (name or "").strip().casefold()
            if not name or key == current_key or key in seen:
                continue
            if item["score"] is not None and item["score"] >= 51:
                continue
            if item["spent"]:
                continue
            return name

        return None

    def _continue_auto_from_results(self, current_title, status, score, elapsed,
                                    seen_titles, continuation_depth):
        try:
            if self.current_chapter_cid:
                rows = self._load_chapter_rows(self.current_chapter_cid)
                if not rows:
                    self.print_status(
                        "ERROR",
                        f"โหลดรายการแบบฝึกหัดของบท {self.current_chapter_name} ไม่สำเร็จ — หยุดเพื่อไม่ให้ข้าม level"
                    )
                    return (status, score, elapsed)
            else:
                if ("/results" not in self.page.url.lower() or
                        "/articles/list" in self.page.url.lower()):
                    if not self.navigate_to_results():
                        return (status, score, elapsed)
                self.page.locator("tbody tr.clickable").first.wait_for(
                    state="visible", timeout=15000
                )
                self._ensure_all_exercises_visible()
                rows = self.page.locator("tbody tr.clickable").all()

            next_title = self._next_uncompleted_exercise_title(
                current_title, rows, seen_titles or set()
            )
            if not next_title:
                self.print_status(
                    "OK",
                    f"ไม่มีแบบฝึกหัดใหม่ที่ยังไม่เคยเริ่มในบท "
                    f"{self.current_chapter_name or '-'}; หยุดเพื่อไม่ให้ทำข้อเดิมซ้ำ",
                )
                return (status, score, elapsed)

            self.print_status(
                "INFO",
                f"เปิดแบบฝึกหัดถัดไปในบท {self.current_chapter_name or '-'}: {next_title}"
            )
            if not self._click_row_safely(next_title):
                self.print_status("ERROR", f"เปิดแบบฝึกหัดถัดไปไม่สำเร็จ: {next_title}")
                return (status, score, elapsed)
            self.page.wait_for_selector(
                ".exercise, .video", state="visible", timeout=15000
            )
            self.current_packet_id = self._url_packet_id() or self.current_packet_id
            next_total = self.get_total_exercises(retries=2, wait_seconds=0.75)
            if next_total == 0:
                self.print_status(
                    "ERROR", f"เปิด {next_title} แล้ว แต่ยังไม่พบหน้าแบบฝึกหัด"
                )
                return (status, score, elapsed)
            return self.auto_solve_exercise(
                next_title,
                selected_pages=None,
                is_continuation=True,
                already_opened=True,
                total_pages=next_total,
                auto_continue=True,
                continuation_depth=continuation_depth + 1,
                chain_seen_titles=seen_titles,
            )
        except Exception as e:
            self.print_status("WAIT", f"กลับหน้า Results เพื่อหาแบบฝึกหัดถัดไปไม่สำเร็จ: {e}")
            return (status, score, elapsed)

    def _click_row_safely(self, title, wait_timeout=15000):
        try:
            row = self.page.locator("tbody tr.clickable").filter(has_text=title).first
            row.wait_for(state="attached", timeout=wait_timeout)
        except Exception as e:
            self.logger.warning(
                f"[CLICK] Row not found within {wait_timeout}ms for '{title}'"
            )
            try:
                total = self.page.locator("tbody tr.clickable").count()
                self.logger.warning(f"[CLICK] Current tbody tr.clickable count: {total}")
            except Exception:
                pass
            return False

        time.sleep(0.5)

        try:
            row.scroll_into_view_if_needed(timeout=3000)
            time.sleep(0.4)
        except Exception as e:
            self.logger.debug(f"[CLICK] scroll_into_view failed: {e}")
            try:
                row.evaluate("el => el.scrollIntoView({block: 'center'})")
                time.sleep(0.5)
            except Exception:
                pass

        try:
            row.click(timeout=3000)
            self.logger.info(f"[CLICK] '{title}' clicked (normal)")
            return True
        except Exception as e:
            self.logger.debug(f"[CLICK] normal click failed: {str(e)[:120]}")

        try:
            row.click(timeout=3000, force=True)
            self.logger.info(f"[CLICK] '{title}' clicked (force)")
            return True
        except Exception as e:
            self.logger.debug(f"[CLICK] force click failed: {str(e)[:120]}")

        try:
            row.evaluate("el => el.click()")
            self.logger.info(f"[CLICK] '{title}' clicked (JS)")
            return True
        except Exception as e:
            self.logger.error(f"[CLICK] JS click failed: {e}")

        return False

    def after_solve_menu(self):
        while True:
            print(f"\n  {Fore.CYAN}--- {self.T['next_action']} ---{Style.RESET_ALL}")
            print(f"  {Fore.WHITE}[1]{Style.RESET_ALL} {self.T['back_to_list']}")
            print(f"  {Fore.WHITE}[2]{Style.RESET_ALL} {self.T['back_to_main']}")
            print(f"  {Fore.WHITE}[3]{Style.RESET_ALL} {self.T['exit_app']}")
            try:
                c = self._input(f"\n  {Fore.CYAN}[>] Select: {Style.RESET_ALL}").strip()
                if c == "1":
                    if self.current_chapter_cid and self.current_chapter_name:
                        self.select_chapter(self.current_chapter_cid, self.current_chapter_name)
                    else:
                        self.navigate_to_results()
                        time.sleep(1)
                        self.fetch_exercises()
                    return
                elif c == "2" or c == "":
                    return
                elif c == "3":
                    self.shutdown()
            except ValueError:
                pass

    def shutdown(self):
       
        self.print_status("INFO", "Exiting...")
        self._save_answer_db_per_chapter(self.current_chapter_name)
        self._cleanup_browser()
        try:
            sys.stdout.flush()
            if hasattr(sys.stdout, 'close'):
                sys.stdout.close()
           
            if hasattr(self, 'original_stdout'):
                sys.stdout = self.original_stdout
        except Exception:
            pass
        sys.exit(0)

    def select_language(self):
        self.print_header()
        print(f"  {Fore.CYAN}--- {self.T['select_lang']} ---{Style.RESET_ALL}\n")
        print(f"  {Fore.WHITE}[1]{Style.RESET_ALL} {self.T['lang_th']}")
        print(f"  {Fore.WHITE}[2]{Style.RESET_ALL} {self.T['lang_en']}")
        try:
            c = input(f"\n  {Fore.CYAN}[>] Select: {Style.RESET_ALL}").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if c == "1":
            self.lang = "th"
        elif c == "2":
            self.lang = "en"
        else:
            return
        self.T = TRANSLATIONS[self.lang]
        try:
            self.logger.info(f"INPUT: {c}")
            self.logger.info(f"LANG: {self.lang}")
        except Exception:
            pass

    def login_menu(self):
        while True:
            self.print_header()
            print(f"  {Fore.CYAN}--- {self.T['login_menu']} ---{Style.RESET_ALL}\n")
            print(f"  {Fore.WHITE}[1]{Style.RESET_ALL} {self.T['opt_login']}")
            print(f"  {Fore.WHITE}[2]{Style.RESET_ALL} {self.T['opt_manage_acc']}")
            print(f"  {Fore.WHITE}[3]{Style.RESET_ALL} {self.T['opt_exit']}")
            try:
                c = self._input(f"\n  {Fore.CYAN}[>] Select: {Style.RESET_ALL}").strip()
                if c == "":
                    continue
                if c == "1":
                    self.accounts = self.load_accounts()
                    u, p = self.select_account()
                    if u:
                        if self.start_browser_and_login(u, p):
                            self.navigate_to_results()
                            return
                elif c == "2":
                    self.accounts = self.load_accounts()
                    self.select_account()
                elif c == "3":
                    self.shutdown()
                else:
                    self.print_status("ERROR", self.T['invalid_choice'])
                    time.sleep(0.5)
            except KeyboardInterrupt:
                self.shutdown()
            except Exception as e:
                self.print_status("ERROR", f"Error: {str(e)}")
                self._input(f"\n  {Fore.CYAN}[>] {self.T['press_enter']}{Style.RESET_ALL}")

    def dashboard_menu(self):
        while True:
            self.print_header()
            print(f"  {Fore.CYAN}--- {self.T['dashboard_menu']} ---{Style.RESET_ALL}\n")
            print(f"  {Fore.WHITE}[1]{Style.RESET_ALL} {self.T['opt_select_chapter']}")
            print(f"  {Fore.WHITE}[2]{Style.RESET_ALL} {self.T['opt_logout']}")
            print(f"  {Fore.WHITE}[3]{Style.RESET_ALL} {self.T['opt_exit']}")
            try:
                c = self._input(f"\n  {Fore.CYAN}[>] Select: {Style.RESET_ALL}").strip()
                if c == "":
                    continue
                if c == "1":
                    self.fetch_and_select_chapter()
                elif c == "2":
                    self.print_status("INFO", "Logging out...")
                    self._cleanup_browser()
                    return
                elif c == "3":
                    self.shutdown()
                else:
                    self.print_status("ERROR", self.T['invalid_choice'])
                    time.sleep(0.5)
            except KeyboardInterrupt:
                self.shutdown()
            except Exception as e:
                self.print_status("ERROR", f"Error: {str(e)}")
                self._input(f"\n  {Fore.CYAN}[>] {self.T['press_enter']}{Style.RESET_ALL}")

    def main_menu(self):
        self.select_language()
        while True:
            if not self.page:
                self.login_menu()
            else:
                self.dashboard_menu()

if __name__ == "__main__":
    bot = SpeexxBotCLI()
    bot.main_menu()
