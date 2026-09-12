// Aloft frontend — shared API client
//
// Include js/config.js BEFORE this file on every page that needs the API.
// Provides:
//   AloftAuth  — token storage (localStorage) + login-state check
//   AloftApi   — typed calls to the backend (signup, login, forgot/reset
//                password, logout, me — more endpoints get added as we
//                wire more pages in later batches)
//
// Every call other than signup/login/forgot-password/reset-password sends
// "Authorization: Bearer <access_token>" automatically. If a call comes
// back 401, we try one silent refresh (POST /v1/auth/refresh) and retry
// the original request once before giving up.

const AloftAuth = {
  ACCESS_KEY: 'aloft_access_token',
  REFRESH_KEY: 'aloft_refresh_token',

  getAccessToken() {
    return localStorage.getItem(this.ACCESS_KEY);
  },
  getRefreshToken() {
    return localStorage.getItem(this.REFRESH_KEY);
  },
  setTokens(accessToken, refreshToken) {
    if (accessToken) localStorage.setItem(this.ACCESS_KEY, accessToken);
    if (refreshToken) localStorage.setItem(this.REFRESH_KEY, refreshToken);
  },
  clearTokens() {
    localStorage.removeItem(this.ACCESS_KEY);
    localStorage.removeItem(this.REFRESH_KEY);
  },
  isLoggedIn() {
    return !!this.getAccessToken();
  },
};

async function aloftTryRefresh() {
  const refreshToken = AloftAuth.getRefreshToken();
  if (!refreshToken) return false;
  try {
    const res = await fetch(`${window.ALOFT_API_BASE}/v1/auth/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
    if (!res.ok) {
      AloftAuth.clearTokens();
      return false;
    }
    const data = await res.json();
    AloftAuth.setTokens(data.access_token, data.refresh_token);
    return true;
  } catch (err) {
    AloftAuth.clearTokens();
    return false;
  }
}

/**
 * Core request helper.
 * @param {string} path - e.g. '/v1/auth/login'
 * @param {object} opts - { method, body, auth, retry }
 */
async function aloftApiRequest(path, opts = {}) {
  const { method = 'GET', body = null, auth = true, retry = true } = opts;

  const headers = { 'Content-Type': 'application/json' };
  if (auth) {
    const token = AloftAuth.getAccessToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;
  }

  let res;
  try {
    res = await fetch(`${window.ALOFT_API_BASE}${path}`, {
      method,
      headers,
      body: body !== null ? JSON.stringify(body) : undefined,
    });
  } catch (networkErr) {
    const err = new Error(
      'Could not reach the Aloft server. Is the backend running on ' +
        window.ALOFT_API_BASE +
        '?'
    );
    err.status = 0;
    err.cause = networkErr;
    throw err;
  }

  if (res.status === 401 && auth && retry && AloftAuth.getRefreshToken()) {
    const refreshed = await aloftTryRefresh();
    if (refreshed) {
      return aloftApiRequest(path, { method, body, auth, retry: false });
    }
  }

  let data = null;
  try {
    data = await res.json();
  } catch (_) {
    // No JSON body (e.g. 204) — that's fine.
  }

  if (!res.ok) {
    const detail = data && (data.detail || data.message);
    let message;
    if (Array.isArray(detail)) {
      // FastAPI validation errors come back as a list of {msg, loc, ...}
      message = detail.map((d) => d.msg || JSON.stringify(d)).join('; ');
    } else {
      message = detail || `Request failed (${res.status})`;
    }
    const err = new Error(message);
    err.status = res.status;
    err.data = data;
    throw err;
  }

  return data;
}

const AloftApi = {
  signup(email, password) {
    return aloftApiRequest('/v1/auth/signup', {
      method: 'POST',
      body: { email, password },
      auth: false,
    });
  },

  async login(email, password) {
    const data = await aloftApiRequest('/v1/auth/login', {
      method: 'POST',
      body: { email, password },
      auth: false,
    });
    AloftAuth.setTokens(data.access_token, data.refresh_token);
    return data;
  },

  forgotPassword(email) {
    return aloftApiRequest('/v1/auth/forgot-password', {
      method: 'POST',
      body: { email },
      auth: false,
    });
  },

  resetPassword(token, newPassword) {
    return aloftApiRequest('/v1/auth/reset-password', {
      method: 'POST',
      body: { token, new_password: newPassword },
      auth: false,
    });
  },

  async logout() {
    const refreshToken = AloftAuth.getRefreshToken();
    try {
      if (refreshToken) {
        await aloftApiRequest('/v1/auth/logout', {
          method: 'POST',
          body: { refresh_token: refreshToken },
        });
      }
    } finally {
      AloftAuth.clearTokens();
    }
  },

  me() {
    return aloftApiRequest('/v1/auth/me');
  },
};
