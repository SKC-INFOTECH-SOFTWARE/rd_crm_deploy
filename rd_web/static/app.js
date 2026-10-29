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
