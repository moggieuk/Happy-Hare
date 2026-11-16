// Transport only: replay state and rendering belong to the caller.

// Recording is decided by Klipper (MMU_TELEMETRY) and published as the mmu.telemetry_mode status variable, so the
// control only shows on a printer's stream and reflects that variable. The simulator and loaded files have none.
export const recordControl=({viewMode,offline,simulator,connected,mode})=>({
  visible:viewMode==='playback' && !offline && !simulator && Boolean(connected),
  value:mode || 'off'});
export class TelemetryStream {
  constructor(rpc, accept, error) {
    Object.assign(this,{rpc,accept,error,generation:0,active:false});
  }
  async start(history=this.history || 'start', checkpoint=false) {
    this.history=history;
    const generation=++this.generation;
    this.active=true; this.ready=false; this.stream=null; this.delivery=this.acknowledged=0;
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
    const ack=()=>this.rpc('server.mmu.telemetry.ack',{stream_id:batch.stream_id,delivery_id:batch.delivery_id});
    try {
      // The sender repeats a batch whose acknowledgement is late or lost: never replay it twice
      if (batch.stream_id===this.stream && batch.delivery_id<=this.delivery) {
        if (batch.delivery_id<=this.acknowledged) await ack();
        return;
      }
      this.stream=batch.stream_id; this.delivery=batch.delivery_id;
      const reset=await this.accept(batch);
      if (!this.active || !this.ready || generation!==this.generation) return;
      if (reset) return this.start(this.history,true); // Renew at current state, without rereading old history.
      this.acknowledged=batch.delivery_id;
      await ack();
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
