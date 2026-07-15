# Weft julia kernel driver — same file protocol as driver.py.
n = 0
while true
    if isfile("kernel.stop")
        exit(0)
    end
    rc_f = "blocks/" * lpad(n, 4, '0') * ".rc"
    code_f = "blocks/" * lpad(n, 4, '0') * ".code"
    if isfile(rc_f)
        global n += 1
        continue
    end
    if !isfile(code_f)
        sleep(0.2)
        continue
    end
    write("current_block", string(n))
    art = "blocks/" * lpad(n, 4, '0') * ".artifacts"
    mkpath(art)
    ENV["WEFT_BLOCK_DIR"] = art
    rc = 0
    # real files from block start, flushed on a timer: a controller
    # tailing them streams output while the block runs
    out = open("blocks/" * lpad(n, 4, '0') * ".out", "w")
    err = open("blocks/" * lpad(n, 4, '0') * ".err", "w")
    flusher = Timer(0.5; interval=0.5) do _
        try flush(out); flush(err) catch end
    end
    try
        redirect_stdio(stdout=out, stderr=err) do
            include_string(Main, read(code_f, String), "block-$n")
        end
    catch e
        rc = e isa InterruptException ? 130 : 1
        print(err, sprint(showerror, e))
    finally
        close(flusher)
        close(out); close(err)
    end
    write(rc_f * ".tmp", string(rc))
    mv(rc_f * ".tmp", rc_f, force=true)
    rm("current_block", force=true)
    global n += 1
end
