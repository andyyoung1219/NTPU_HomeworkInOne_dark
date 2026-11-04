"""Shared presentation for the two centers and the homework wizard."""

import gradio as gr

STEPS = ("選擇作業", "讀取資料", "設定 AI", "產生草稿", "審閱匯出")
PAGES = ("home", "hub", "select", "sources", "configure", "generate", "review")


def page_updates(page):
    if page not in PAGES:
        raise ValueError("Unknown page")
    return [gr.update(visible=name == page) for name in PAGES]


def step_heading(index, title, description):
    steps = "".join(
        f'<li class="{"current" if i == index else "done" if i < index else ""}"'
        f'{" aria-current=\"step\"" if i == index else ""}>'
        f'<span>{"✓" if i < index else i + 1:}</span>{label}</li>'
        for i, label in enumerate(STEPS)
    )
    return (f'<nav aria-label="作業進度"><ol class="steps">{steps}</ol></nav>'
            f'<div class="page-heading"><p class="eyebrow">HOMEWORK / 0{index + 1}</p>'
            f'<h1 tabindex="-1">{title}</h1><p>{description}</p></div>')


CSS = """
.dashboard-stats {display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:14px;margin:10px 0 20px}
.dashboard-stats > div {padding:18px;border:1px solid #2c4350;border-radius:14px;background:#14232d}
.dashboard-stats span {display:block;color:#a4b7c5;font-size:13px}
.dashboard-stats strong {display:block;color:#79e0bf;font-size:30px;margin-top:8px}
.gradio-container {max-width:1180px!important;margin:auto!important;padding:24px!important}
body, .gradio-container {background:#0b1219!important}
#app-bar {align-items:center;border-bottom:1px solid #263642;padding-bottom:18px;margin-bottom:24px}
#app-bar > .block {flex:4 1 240px!important;min-width:240px!important}
#brand {font-size:17px;font-weight:700;letter-spacing:1px;color:#e2eee9;white-space:nowrap}
#app-bar button {min-height:42px}
.eyebrow {font-size:12px!important;letter-spacing:2.5px;color:#71dab7!important;font-weight:700}
.page-heading {padding:12px 0 24px}
.page-heading h1 {font-size:32px!important;line-height:1.4!important;color:#f1f7f5;margin:8px 0!important}
.page-heading p {color:#a4b7c5;font-size:15px;line-height:1.8}
#welcome {padding:42px 0 30px;max-width:740px}
#welcome h1 {font-size:44px!important;letter-spacing:-1px}
.center-card {border:1px solid #2c4350!important;border-radius:20px!important;
 background:linear-gradient(145deg,#172832,#101c26)!important;padding:30px!important;gap:20px!important}
.center-card h2 {font-size:27px!important;color:#edf5f2;margin:16px 0!important}
.center-card p {color:#b3c4ce;line-height:1.9}
.center-card button {margin-top:12px;min-height:52px;font-size:16px!important}
.card-icon {font-size:28px;color:#79e0bf;width:56px;height:56px;border-radius:16px;
 background:#203f40;display:flex;align-items:center;justify-content:center}
.card-meta {font-size:12px;color:#84a99c;letter-spacing:1px;margin-top:24px}
.home-note {color:#829aa9;font-size:13px;padding-top:26px;line-height:1.8}
.steps {display:grid;grid-template-columns:repeat(5,minmax(0,1fr));align-items:stretch;
 list-style:none!important;padding:0!important;gap:8px;margin:0 0 26px!important}
.steps > li {display:flex;align-items:center;gap:8px;color:#8a9eae;font-size:13px;
 box-sizing:border-box;min-width:0;line-height:26px!important;
 margin:0!important;border-bottom:2px solid #253440;padding:12px 0!important}
.steps li span {display:inline-flex;align-items:center;justify-content:center;
 width:26px;height:26px;border-radius:50%;background:#20313d;flex-shrink:0}
.steps .current {color:#89efd0;border-color:#71dab7;font-weight:700}
.steps .current span {background:#245749;color:#acffe2}
.steps .done {color:#b4c9bf;border-color:#3d7462}
.surface {border:1px solid #293e4a!important;border-radius:16px!important;padding:22px!important;background:#111e28!important}
.page-actions {margin-top:20px!important;padding-top:18px;border-top:1px solid #263642}
.prose {line-height:1.8}
textarea {line-height:1.7!important}
button {transition:background .15s ease,border-color .15s ease!important}
button:focus-visible, input:focus-visible {outline:2px solid #91f4d0!important;outline-offset:3px}
#connection-modal {position:fixed!important;inset:0!important;z-index:1000!important;
 background:rgba(3,9,15,.82)!important;backdrop-filter:blur(8px);overflow-y:auto;
 padding:6vh max(20px,calc((100vw - 620px)/2))!important;box-sizing:border-box;display:block}
#connection-panel {padding:26px!important;border:1px solid #426052!important;
 border-radius:20px!important;background:#14212c!important;box-shadow:0 30px 100px #0008}
#connection-panel h2 {font-size:25px!important}
footer {display:none!important}
@media(max-width:760px) {
 #app-bar > :first-child {flex-basis:100%!important}
 .gradio-container {padding:14px!important}
 #welcome {padding:20px 0} #welcome h1 {font-size:32px!important}
 .center-card {padding:22px!important}
 .steps {gap:4px}.steps li {font-size:11px;flex-direction:column;gap:6px;text-align:center}
 .page-heading h1 {font-size:27px!important}
 #connection-modal {padding:14px!important}.surface {padding:14px!important}
}
"""

# Focus the new screen; keep keyboard focus inside the connection dialog.
SHELL_JS = """
const root = document.querySelector('.gradio-container');
let dialogOpen = false;
let previousFocus;
let currentPage;
const visible = el => el && el.getClientRects().length > 0;
const update = () => {
  const modal = document.getElementById('connection-modal');
  const opened = visible(modal);
  if (opened !== dialogOpen) {
    dialogOpen = opened;
    for (const el of root.querySelectorAll('.app-page, #app-bar')) el.inert = opened;
    if (opened) {
      previousFocus = document.activeElement;
      modal.setAttribute('role', 'dialog');
      modal.setAttribute('aria-modal', 'true');
      modal.setAttribute('aria-label', '連線管理');
      modal.querySelector('button')?.focus();
    } else previousFocus?.focus();
  }
  const page = [...root.querySelectorAll('.app-page')].find(visible);
  if (page && page.id !== currentPage && !opened) {
    currentPage = page.id;
    window.scrollTo({top:0, behavior:'instant'});
    page.setAttribute('tabindex', '-1');
    (page.querySelector('h1') || page).focus({preventScroll:true});
  }
};
new MutationObserver(update).observe(root, {childList:true, subtree:true, attributes:true,
  attributeFilter:['style','class','hidden','id']});
root.addEventListener('keydown', e => {
  if (!dialogOpen) return;
  if (e.key === 'Escape') document.querySelector('#close-connections')?.click();
  if (e.key !== 'Tab') return;
  const items = [...document.querySelectorAll('#connection-modal button, #connection-modal input, #connection-modal a')]
    .filter(el => visible(el) && !el.disabled && el.tabIndex >= 0);
  const first = items[0], last = items[items.length-1];
  if (e.shiftKey && document.activeElement === first) {e.preventDefault();last?.focus();}
  if (!e.shiftKey && document.activeElement === last) {e.preventDefault();first?.focus();}
});
update();
"""
