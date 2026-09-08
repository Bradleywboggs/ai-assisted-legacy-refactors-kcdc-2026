<?php

class Cx
{
    private $p;
    private $n = 0;

    public function __construct(PDO $p) { $this->p = $p; }

    public function begin()
    {
        $this->n++;
        if ($this->n === 1) { $this->p->beginTransaction(); }
        return true;
    }

    public function commit()
    {
        if ($this->n === 0) { return false; }
        $this->n--;
        if ($this->n === 0) { $this->p->commit(); }
        return true;
    }

    public function rollBack()
    {
        if ($this->n === 0) { return false; }
        $this->n = 0;
        $this->p->rollBack();
        return true;
    }

    public function inTx() { return $this->n > 0; }

    public function ex($sql, $b = [])
    {
        $s = $this->p->prepare($sql);
        $s->execute($b);
        return $s;
    }

    public function q($sql, $b = [])
    {
        $s = $this->p->prepare($sql);
        $s->execute($b);
        return $s;
    }

    public function lastId() { return $this->p->lastInsertId(); }
}

function conn()
{
    $h = getenv('DB_HOST') ?: '127.0.0.1';
    $d = getenv('DB_NAME') ?: 'evse';
    $u = getenv('DB_USER') ?: 'ingest_svc';
    $w = getenv('DB_PASS') ?: 'ingest_svc';
    $p = getenv('DB_PORT') ?: '3306';
    $o = new PDO("mysql:host={$h};port={$p};dbname={$d};charset=utf8mb4", $u, $w, [
        PDO::ATTR_ERRMODE            => PDO::ERRMODE_EXCEPTION,
        PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
        PDO::ATTR_EMULATE_PREPARES   => false,
    ]);
    return new Cx($o);
}

function say($s)
{
    fwrite(STDOUT, '[' . getmypid() . '] ' . $s . "\n");
}
