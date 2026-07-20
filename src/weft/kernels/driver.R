# Weft R kernel driver — same file protocol as driver.py.
# Lazy-session forward hook: WEFT_SESSION_RLIB names the session's rlib
# layer (may not exist yet). Create + front-load it on .libPaths so a
# later session_install(cran=...) is visible to THIS kernel's next
# library() call — R scans lib dirs live, no cache to invalidate.
rlib <- Sys.getenv("WEFT_SESSION_RLIB")
if (nzchar(rlib)) {
  dir.create(rlib, showWarnings = FALSE, recursive = TRUE)
  .libPaths(c(rlib, .libPaths()))
}
env <- new.env(parent = globalenv())
n <- 0L
repeat {
  if (file.exists("kernel.stop")) quit(save = "no", status = 0)
  rc_f <- sprintf("blocks/%04d.rc", n)
  code_f <- sprintf("blocks/%04d.code", n)
  if (file.exists(rc_f)) { n <- n + 1L; next }
  if (!file.exists(code_f)) { Sys.sleep(0.2); next }
  writeLines(as.character(n), "current_block")
  art <- sprintf("blocks/%04d.artifacts", n)
  dir.create(art, showWarnings = FALSE, recursive = TRUE)
  Sys.setenv(WEFT_BLOCK_DIR = art)
  out_f <- sprintf("blocks/%04d.out", n)
  err_f <- sprintf("blocks/%04d.err", n)
  rc <- 0L
  out_con <- file(out_f, open = "wt"); err_con <- file(err_f, open = "wt")
  # created empty NOW; flushed between top-level expressions so a
  # controller tailing the files streams statement-by-statement (R
  # connections buffer internally — within one long expression output
  # still arrives when it completes; that is the honest base-R limit)
  flush(out_con); flush(err_con)
  sink(out_con, type = "output"); sink(err_con, type = "message")
  tryCatch({
    exprs <- parse(text = paste(readLines(code_f), collapse = "\n"))
    for (e in exprs) {   # same semantics as eval(exprs): no auto-print
      eval(e, envir = env)
      flush(out_con); flush(err_con)
    }
  }, interrupt = function(e) {
    rc <<- 130L; message("[interrupted]")
  }, error = function(e) {
    rc <<- 1L; message(conditionMessage(e))
  })
  sink(type = "message"); sink(type = "output")
  close(out_con); close(err_con)
  writeLines(as.character(rc), paste0(rc_f, ".tmp"))
  file.rename(paste0(rc_f, ".tmp"), rc_f)
  unlink("current_block")
  n <- n + 1L
}
