# Weft julia kernel driver — same file protocol as driver.py.
# the jobdir, captured BEFORE any user code: a block's cd() persists
# (session state) but cannot orphan the driver's protocol files
const JOBDIR = pwd()
jp(rel) = joinpath(JOBDIR, rel)
# driver.ready: activation done, loop live (atomic; see driver.py)
open(jp("driver.ready.tmp"), "w") do f; write(f, "1\n"); end
mv(jp("driver.ready.tmp"), jp("driver.ready"); force=true)
n = 0
while true
    if isfile(jp("kernel.stop"))
        exit(0)
    end
    rc_f = jp("blocks/" * lpad(n, 4, '0') * ".rc")
    code_f = jp("blocks/" * lpad(n, 4, '0') * ".code")
    if isfile(rc_f)
        global n += 1
        continue
    end
    if !isfile(code_f)
        sleep(0.2)
        continue
    end
    write(jp("current_block"), string(n))
    art = jp("blocks/" * lpad(n, 4, '0') * ".artifacts")
    mkpath(art)
    ENV["WEFT_BLOCK_DIR"] = art
    rc = 0
    # real files from block start, flushed on a timer: a controller
    # tailing them streams output while the block runs
    out = open(jp("blocks/" * lpad(n, 4, '0') * ".out"), "w")
    err = open(jp("blocks/" * lpad(n, 4, '0') * ".err"), "w")
    flusher = Timer(0.5; interval=0.5) do _
        try flush(out); flush(err) catch end
    end
    try
        redirect_stdio(stdout=out, stderr=err) do
            # REPL convention: show the final value unless nothing —
            # agents expect what the julia prompt shows
            res = include_string(Main, read(code_f, String), "block-$n")
            if res !== nothing
                show(stdout, MIME"text/plain"(), res)
                println()
            end
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
    rm(jp("current_block"), force=true)
    global n += 1
end
