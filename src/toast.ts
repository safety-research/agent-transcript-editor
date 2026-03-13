/** Imperative DOM-based toast notification. Reuses existing .toast-container CSS. */
export function showToast(message: string, type: 'success' | 'info' | 'warning' = 'info') {
  let container = document.querySelector('.toast-container');
  if (!container) {
    container = document.createElement('div');
    container.className = 'toast-container';
    document.body.appendChild(container);
  }

  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.textContent = message;
  container.appendChild(toast);

  setTimeout(() => {
    toast.remove();
    // Clean up container if empty
    if (container && container.childElementCount === 0) {
      container.remove();
    }
  }, 3000);
}
