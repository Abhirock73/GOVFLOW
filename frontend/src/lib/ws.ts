export class GovFlowWebSocket {
  private ws: WebSocket | null = null;
  private url: string;
  private listeners: Set<(data: any) => void> = new Set();
  private reconnectTimer: any = null;

  constructor(jobId: string) {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = window.location.host;
    // In dev vite proxy or direct connection
    const wsHost = host.includes(':5173') ? '127.0.0.1:8000' : host;
    this.url = `${protocol}//${wsHost}/jobs/${jobId}/stream`;
    this.connect();
  }

  private connect() {
    try {
      this.ws = new WebSocket(this.url);
      this.ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          this.listeners.forEach((fn) => fn(data));
        } catch (e) {
          console.error('Failed to parse WS message:', e);
        }
      };

      this.ws.onclose = () => {
        this.reconnectTimer = setTimeout(() => this.connect(), 2000);
      };

      this.ws.onerror = (err) => {
        console.error('WebSocket error:', err);
      };
    } catch (e) {
      console.error('WebSocket connect error:', e);
    }
  }

  public subscribe(listener: (data: any) => void) {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  public send(data: any) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(data));
    }
  }

  public sendTakeoverBegin(reason: string = 'User takeover') {
    this.send({ type: 'takeover_begin', reason });
  }

  public sendTakeoverEnd() {
    this.send({ type: 'takeover_end' });
  }

  public sendTakeoverInput(event: any) {
    this.send({ type: 'takeover_input', event });
  }

  public sendChatMessage(text: string) {
    this.send({ type: 'chat_message', text });
  }

  public close() {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    if (this.ws) {
      this.ws.onclose = null;
      this.ws.close();
    }
  }
}

