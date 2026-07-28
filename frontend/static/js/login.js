/* Login page script.
 *
 * The server sets HttpOnly session + CSRF cookies on success.
 * No token is stored in localStorage or JavaScript state.
 */
document.getElementById('loginForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const username = document.getElementById('username').value;
    const password = document.getElementById('password').value;
    const errorEl = document.getElementById('error');

    try {
        const formData = new URLSearchParams();
        formData.append('username', username);
        formData.append('password', password);

        const res = await fetch('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: formData,
            credentials: 'same-origin'
        });

        if (res.ok) {
            window.location.href = '/';
        } else {
            errorEl.textContent = 'Invalid credentials';
            errorEl.classList.add('show');
        }
    } catch (err) {
        errorEl.textContent = 'Connection error';
        errorEl.classList.add('show');
    }
});
