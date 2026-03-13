// Backend API URL (proxied through Vite in development)
const API_BASE = '/api';

export async function testConnection(): Promise<{ success: boolean; error?: string }> {
  try {
    const response = await fetch(`${API_BASE}/llm/test`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
    });

    const result = await response.json();
    return result;
  } catch (e) {
    return { success: false, error: String(e) };
  }
}

export async function checkApiStatus(): Promise<{ configured: boolean; alt_configured?: boolean }> {
  try {
    const response = await fetch(`${API_BASE}/llm/status`);
    return await response.json();
  } catch {
    return { configured: false };
  }
}
