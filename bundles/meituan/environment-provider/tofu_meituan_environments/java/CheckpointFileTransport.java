/* Sealed snapshot uploader for an admitted Hadoop backend. Entry: upload local
 * destination bytes SHA lanes. Host pins the built Java6 JAR; no compilation or
 * network occurs on provider import. Readback verification remains independent. */
import org.apache.hadoop.conf.Configuration;
import org.apache.hadoop.fs.*;
import java.io.*;
import java.security.MessageDigest;
import java.util.*;
import java.util.concurrent.*;
public class CheckpointFileTransport {
 static String hex(byte[] b){StringBuilder s=new StringBuilder();for(byte v:b)s.append(String.format("%02x",v&255));return s.toString();}
 static void verify(FileSystem fs,Path target,long size,String digest)throws Exception {
  MessageDigest hash=MessageDigest.getInstance("SHA-256");byte[] b=new byte[1048576];long count=0;FSDataInputStream in=fs.open(target);
  try {int n;while((n=in.read(b))!=-1){hash.update(b,0,n);count+=n;}}finally{in.close();}
  if(count!=size||!hex(hash.digest()).equals(digest))throw new IOException("remote integrity mismatch");
  System.out.println("FILE_VERIFIED_BYTES="+count);
 }
 public static void main(String[] args)throws Exception {
  if(args[0].equals("prepare")){
   File local=new File(args[1]);if(!local.createNewFile())throw new IOException("local exists");
   OutputStream out=new FileOutputStream(local);byte[] b=new byte[65536];Random rng=new Random(17);MessageDigest hash=MessageDigest.getInstance("SHA-256");
   try {for(int i=0;i<8192;i++){rng.nextBytes(b);out.write(b);hash.update(b);}}finally{out.close();}
   System.out.println("LOCAL_SHA256="+hex(hash.digest()));return;
  }
  final File local=new File(args[1]);final long size=Long.parseLong(args[3]);String digest=args[4];
  final Path target=new Path(args[2]);final FileSystem fs=target.getFileSystem(new Configuration());
  if(args[0].equals("verify")){verify(fs,target,size,digest);return;}
  if(!args[0].equals("upload")||local.length()!=size||fs.exists(target)||size<=0)throw new IOException("invalid input");
  int lanes=Integer.parseInt(args[5]);if(lanes<1)throw new IOException("invalid concurrency");
  final long block=134217728L;final int count=(int)((size+block-1)/block);
  final Path[] parts=new Path[count];final String nonce=UUID.randomUUID().toString();
  for(int i=0;i<count;i++)parts[i]=new Path(target.getParent(),target.getName()+".tofu-part-"+nonce+"-"+i);
  ExecutorService pool=Executors.newFixedThreadPool(Math.min(lanes,count));long start=System.nanoTime();
  try {
   List<Future<Void>> futures=new ArrayList<Future<Void>>();
   for(int i=0;i<count;i++){final int index=i;futures.add(pool.submit(new Callable<Void>(){public Void call()throws Exception {
    RandomAccessFile input=new RandomAccessFile(local,"r");FSDataOutputStream output=null;
    try {input.seek(index*block);output=fs.create(parts[index],false,65536,fs.getDefaultReplication(parts[index]),block);byte[] b=new byte[65536];long remaining=Math.min(block,size-index*block);
     while(remaining>0){int n=input.read(b,0,(int)Math.min(b.length,remaining));if(n<0)throw new EOFException("snapshot truncated");output.write(b,0,n);remaining-=n;}
    }finally{try{if(output!=null)output.close();}finally{input.close();}}
    return null;
   }}));}
   for(Future<Void> f:futures)f.get();
  }finally{pool.shutdownNow();if(!pool.awaitTermination(30,TimeUnit.SECONDS))throw new IOException("writers not reaped");}
  double writeSeconds=(System.nanoTime()-start)/1e9;
  Path resolved=fs.resolvePath(parts[0]);FileSystem actual=resolved.getFileSystem(new Configuration());Path[] sources=new Path[count-1];
  for(int i=1;i<count;i++){sources[i-1]=fs.resolvePath(parts[i]);if(!sources[i-1].getParent().equals(resolved.getParent()))throw new IOException("scope changed");}
  long mergeStart=System.nanoTime();if(sources.length>0)actual.concat(resolved,sources);
  if(fs.getFileStatus(parts[0]).getLen()!=size||!fs.rename(parts[0],target))throw new IOException("finalize failed");
  for(Path part:parts)if(fs.exists(part))throw new IOException("source retained");
  System.out.println("FILE_UPLOAD_RESULT={\"bytes\":"+size+",\"write_seconds\":"+writeSeconds+",\"merge_and_rename_seconds\":"+((System.nanoTime()-mergeStart)/1e9)+"}");
 }
}
