# Weft R kernel driver — same file protocol as driver.py.
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
  sink(out_con, type = "output"); sink(err_con, type = "message")
  tryCatch({
    eval(parse(text = paste(readLines(code_f), collapse = "\n")), envir = env)
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
