'use strict';
const base = document.documentElement.dataset.base;
document.querySelector('#login-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.target.querySelector('button');
  const error = document.querySelector('#login-error');
  button.disabled = true; error.textContent = '';
  try {
    const response = await fetch(base + 'api/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({password:document.querySelector('#password').value})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Не удалось войти');
    document.querySelector('#password').value = '';
    location.replace(base);
  } catch (e) { error.textContent = e.message; }
  finally { button.disabled = false; }
});
