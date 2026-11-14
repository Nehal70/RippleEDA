// Synchronous FWFT FIFO (reference solution)
module sync_fifo #(
  parameter int WIDTH = 8,
  parameter int DEPTH = 8          // power of two
) (
  input  logic                   clk,
  input  logic                   rst_n,
  input  logic                   wr_en,
  input  logic                   rd_en,
  input  logic [WIDTH-1:0]       wdata,
  output logic [WIDTH-1:0]       rdata,
  output logic                   full,
  output logic                   empty,
  output logic [$clog2(DEPTH):0] count
);
  localparam int AW = $clog2(DEPTH);
  localparam logic [AW:0] DEPTH_C = DEPTH[AW:0];

  logic [WIDTH-1:0] mem [DEPTH];
  logic [AW-1:0]    wptr, rptr;
  logic             do_wr, do_rd;

  assign do_wr = wr_en;
  assign do_rd = rd_en;
  assign full  = (count == DEPTH_C);
  assign empty = (count == '0);
  assign rdata = mem[rptr];

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      rptr  <= '0;
      count <= '0;
    end else begin
      if (do_wr) wptr <= wptr + 1'b1;
      if (do_rd) rptr <= rptr + 1'b1;
      case ({do_wr, do_rd})
        2'b10:   count <= count + 1'b1;
        2'b01:   count <= count - 1'b1;
        default: count <= count;
      endcase
    end
  end

  always_ff @(posedge clk) begin
    if (do_wr) mem[wptr] <= wdata;
  end
endmodule
