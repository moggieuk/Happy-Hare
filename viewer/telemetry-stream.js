// Transport only: replay state and rendering belong to the caller.
export class TelemetryStream {
  constructor(rpc, accept, error) {
    Object.assign(this,{rpc,accept,error,generation:0,active:false});
  }
  async start(history=this.history || 'start', checkpoint=false) {
    this.history=history;
    const generation=++this.generation;
    this.active=true; this.ready=false;
    try {
      const batch=await this.rpc('server.mmu.telemetry.subscribe',{history:checkpoint ? 'current' : history});
      if (this.active && generation===this.generation) {
        this.ready=true;
        await this.receive(batch,generation);
      }
    } catch(error) {
      if (generation===this.generation) { this.active=false; this.error(error); }
    }
  }
  async receive(batch, generation=this.generation) {
    if (!this.active || !this.ready || generation!==this.generation) return;
    try {
      const reset=await this.accept(batch);
      if (!this.active || !this.ready || generation!==this.generation) return;
      if (reset) return this.start(this.history,true); // Renew at current state, without rereading old history.
      await this.rpc('server.mmu.telemetry.ack',{
        stream_id:batch.stream_id,delivery_id:batch.delivery_id});
    } catch(error) {
      if (generation===this.generation) { await this.stop(); this.error(error); }
    }
  }
  async stop(notify=true) {
    const active=this.active;
    this.active=false; this.generation++;
    if (active && notify) await this.rpc('server.mmu.telemetry.unsubscribe').catch(()=>{});
  }
}
