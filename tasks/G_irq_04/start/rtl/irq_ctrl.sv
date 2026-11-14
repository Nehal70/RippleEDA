// 8-source interrupt controller.
//  edge_mode[i]=1: pending[i] latches on a rising edge of src[i] (even while en[i]=0) and stays set until acknowledged.
//  edge_mode[i]=0: level source, pending[i] = src[i] (not sticky, ack has no effect).
//  irq = |(pending & en); id = lowest index i with pending[i] & en[i] (0 when none).
//  ack (one cycle, while irq=1) clears the edge-pending bit of the current id; an edge arriving in the same cycle wins.
module irq_ctrl (
  input  logic       clk,
  input  logic       rst_n,
  input  logic [7:0] src,
  input  logic [7:0] en,
  input  logic [7:0] edge_mode,
  input  logic       ack,
  output logic       irq,
  output logic [2:0] id,
  output logic [7:0] pending
);
  logic [7:0] prev, epend, masked, clr, rise;

  assign rise    = src & ~prev & en;
  assign pending = (epend & edge_mode) | (src & ~edge_mode);
  assign masked  = pending & en;
  assign irq     = |pending;

  always_comb begin
    id = 3'd0;
    for (int i = 0; i < 8; i++) if (pending[i]) id = 3'(i);
  end

  always_comb begin
    clr = '0;
    if (ack && irq) clr[id] = 1'b1;
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      prev  <= '0;
      epend <= '0;
    end else begin
      prev  <= src;
      epend <= (epend & ~clr) | (src & edge_mode);
    end
  end
endmodule
