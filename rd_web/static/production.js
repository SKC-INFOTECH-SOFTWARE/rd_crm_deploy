// Dragging submits the same CSRF-protected, version-checked form as the stage menu.
(() => {
  const board = document.querySelector('.pipeline-board');
  if (!board) return;
  // The CSP blocks inline style attributes, so sizes are applied here from data attributes.
  board.style.setProperty('--pipeline-columns', board.dataset.columns);
  board.querySelectorAll('[data-progress]').forEach(bar => {
    bar.style.width = `${Number(bar.dataset.progress) || 0}%`;
  });
  let dragged = null;
  board.addEventListener('dragstart', event => {
    const card = event.target.closest('.pipeline-order[draggable="true"]');
    if (!card || event.target.closest('input, select, button, a, form')) {
      event.preventDefault();
      return;
    }
    dragged = card;
    event.dataTransfer.setData('text/plain', card.dataset.order);
    event.dataTransfer.effectAllowed = 'move';
    card.classList.add('is-dragging');
  });
  board.addEventListener('dragover', event => {
    const lane = event.target.closest('.pipeline-column');
    if (!dragged || !lane) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    board.querySelectorAll('.is-drop-target').forEach(el => el.classList.remove('is-drop-target'));
    lane.classList.add('is-drop-target');
  });
  board.addEventListener('drop', event => {
    const lane = event.target.closest('.pipeline-column');
    if (!dragged || !lane) return;
    event.preventDefault();
    const form = dragged.querySelector('.pipeline-stage-form');
    const stage = form?.elements.namedItem('stage');
    if (form && stage && stage.value !== lane.dataset.stage) {
      stage.value = lane.dataset.stage;
      form.requestSubmit();
    }
    board.querySelectorAll('.is-drop-target').forEach(el => el.classList.remove('is-drop-target'));
  });
  board.addEventListener('dragend', () => {
    dragged?.classList.remove('is-dragging');
    dragged = null;
    board.querySelectorAll('.is-drop-target').forEach(el => el.classList.remove('is-drop-target'));
  });
})();
