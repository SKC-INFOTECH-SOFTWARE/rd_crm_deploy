const menu = document.getElementById('mobile-menu');
const sidebar = document.getElementById('sidebar');
const shade = document.getElementById('sidebar-shade');
function setMenu(open) {
  sidebar.classList.toggle('open', open);
  shade.hidden = !open;
  menu.setAttribute('aria-expanded', String(open));
  document.body.classList.toggle('menu-open', open);
}
menu?.addEventListener('click', () => setMenu(!sidebar.classList.contains('open')));
shade?.addEventListener('click', () => setMenu(false));
sidebar?.querySelectorAll('a').forEach(link => link.addEventListener('click', () => setMenu(false)));
document.addEventListener('keydown', event => { if (event.key === 'Escape') setMenu(false); });

const recipeRows = document.querySelector('[data-recipe-rows]');
const recipeTemplate = document.getElementById('recipe-ingredient-template');
document.querySelector('[data-add-recipe-ingredient]')?.addEventListener('click', () => {
  if (!recipeRows || !recipeTemplate) return;
  recipeRows.appendChild(recipeTemplate.content.cloneNode(true));
});
// Inline handlers are blocked by the production Content-Security-Policy.
document.addEventListener('click', event => {
  const button = event.target.closest('[data-confirm]');
  if (button && !window.confirm(button.dataset.confirm)) event.preventDefault();
});
document.addEventListener('click', event => {
  const button = event.target.closest('[data-remove-recipe-ingredient]');
  if (button) button.closest('.recipe-ingredient-row')?.remove();
});
document.addEventListener('change', event => {
  if (!event.target.matches('.recipe-ingredient-item')) return;
  const option = event.target.selectedOptions[0];
  const unit = event.target.closest('.recipe-ingredient-row')?.querySelector(
    '.recipe-ingredient-unit');
  if (unit && !unit.value) unit.value = option?.dataset.unit || '';
});
// Bulk voucher approval on Data review. The CSP blocks inline handlers, so it is wired here.
const bulkForm = document.querySelector('[data-bulk-form]');
if (bulkForm) {
  const boxes = () => [...document.querySelectorAll('[data-bulk-row]')];
  const profile = box => box.closest('.bulk-map')?.querySelector('[data-bulk-client]');
  const count = bulkForm.querySelector('[data-bulk-count]');
  const all = bulkForm.querySelector('[data-bulk-all]');
  const refresh = () => {
    const mappable = boxes().filter(box => profile(box)?.value);
    const picked = boxes().filter(box => box.checked).length;
    count.textContent = `${picked} selected`;
    all.checked = mappable.length > 0 && mappable.every(box => box.checked);
    all.indeterminate = picked > 0 && !all.checked;
  };
  all?.addEventListener('change', () => {
    boxes().forEach(box => { box.checked = all.checked && Boolean(profile(box)?.value); });
    refresh();
  });
  document.addEventListener('change', event => {
    if (event.target.matches('[data-bulk-client]')) {
      const chosen = event.target;
      const mark = select => {
        const box = select.closest('.bulk-map')?.querySelector('[data-bulk-row]');
        if (box) box.checked = Boolean(select.value);
      };
      mark(chosen);
      // Same source party, same company: fill the rows that have no choice yet.
      if (chosen.value) {
        document.querySelectorAll('[data-bulk-client]').forEach(select => {
          if (select === chosen || select.value || select.dataset.bulkParty !== chosen.dataset.bulkParty) return;
          if (![...select.options].some(option => option.value === chosen.value)) return;
          select.value = chosen.value;
          mark(select);
        });
      }
    }
    if (event.target.matches('[data-bulk-row], [data-bulk-client]')) refresh();
  });
  bulkForm.addEventListener('submit', event => {
    const picked = boxes().filter(box => box.checked);
    const unmapped = picked.filter(box => !profile(box)?.value);
    if (!picked.length || unmapped.length) {
      event.preventDefault();
      window.alert(picked.length ? `Choose a client profile for ${unmapped.length} selected voucher(s).`
        : 'Select at least one voucher to approve.');
      (unmapped.length ? profile(unmapped[0]) : boxes()[0])?.focus();
    }
  });
  refresh();
}
